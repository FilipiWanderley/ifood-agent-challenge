"""Recipe-candidate workflow state machine and source provenance.

The state machine is deterministic and independent of persistence, MCP and
the LLM. ``DISCOVERED`` doubles as the invalidation target: any change to a
candidate's recipe, purchase quotes or the session's kitchen profile sends the
candidate back to ``DISCOVERED`` so that feasibility and approval must be
re-established.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from sabor_da_maria_agent.domain.kitchen import normalize_label


class CandidateStatus(StrEnum):
    DISCOVERED = "discovered"
    NEEDS_INFORMATION = "needs_information"
    EVALUATED = "evaluated"
    INFEASIBLE = "infeasible"
    FEASIBLE = "feasible"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    COSTED = "costed"
    PRICED = "priced"


_S = CandidateStatus

ALLOWED_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    _S.DISCOVERED: frozenset({_S.NEEDS_INFORMATION, _S.EVALUATED}),
    _S.NEEDS_INFORMATION: frozenset({_S.DISCOVERED}),
    _S.EVALUATED: frozenset({_S.INFEASIBLE, _S.FEASIBLE}),
    _S.INFEASIBLE: frozenset({_S.DISCOVERED}),
    _S.FEASIBLE: frozenset({_S.AWAITING_APPROVAL, _S.DISCOVERED}),
    _S.AWAITING_APPROVAL: frozenset({_S.APPROVED, _S.REJECTED, _S.DISCOVERED}),
    _S.APPROVED: frozenset({_S.COSTED, _S.DISCOVERED}),
    _S.REJECTED: frozenset({_S.DISCOVERED}),
    _S.COSTED: frozenset({_S.PRICED, _S.DISCOVERED}),
    _S.PRICED: frozenset({_S.DISCOVERED}),
}

# Statuses whose supplemental purchases count against the shared budget.
BUDGET_COMMITTED_STATUSES = frozenset({_S.APPROVED, _S.COSTED, _S.PRICED})


class InvalidTransitionError(ValueError):
    """Raised when a workflow transition is not allowed."""


def ensure_transition(current: CandidateStatus, target: CandidateStatus) -> None:
    """Reject any transition not listed in ``ALLOWED_TRANSITIONS``."""
    if target not in ALLOWED_TRANSITIONS[current]:
        allowed = sorted(status.value for status in ALLOWED_TRANSITIONS[current])
        raise InvalidTransitionError(
            f"Invalid transition {current.value!r} -> {target.value!r}. "
            f"Allowed from {current.value!r}: {allowed}."
        )


class SourceEvidence(BaseModel):
    """A recipe source as reported by Hermes ``web_search``.

    The server stores what the agent reported; it never fetches the URL and
    performs no automated verification (``automated_verification`` is
    always ``False``).

    - ``unverified_search_result`` (default): the title, URL and excerpt are
      evidence of a search hit, not proof that the page exists or contains
      the recipe.
    - ``manually_reviewed``: a named person attested having opened and read
      the source. The attestation (who, when, note) is recorded as reported;
      the server cannot authenticate it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    url: AnyHttpUrl
    excerpt: str | None = Field(default=None, max_length=2000)
    retrieved_at: datetime | None = Field(
        default=None,
        description="Search time reported by the agent (timezone-aware).",
    )
    recorded_at: datetime = Field(description="Server time the evidence was stored.")
    discovery_method: Literal["hermes_web_search"] = "hermes_web_search"
    verification_status: Literal["unverified_search_result", "manually_reviewed"] = (
        "unverified_search_result"
    )
    automated_verification: Literal[False] = False
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=200)
    reviewed_at: datetime | None = None
    review_note: str | None = Field(default=None, max_length=1000)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return normalize_label(value)

    @field_validator("excerpt", "review_note")
    @classmethod
    def normalize_excerpt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @field_validator("retrieved_at", "recorded_at", "reviewed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Timestamps must include a timezone offset.")
        return value

    @model_validator(mode="after")
    def validate_review(self) -> "SourceEvidence":
        reviewed = self.verification_status == "manually_reviewed"
        has_review = self.reviewed_by is not None and self.reviewed_at is not None
        if reviewed != has_review or (
            not reviewed and (self.reviewed_by or self.reviewed_at or self.review_note)
        ):
            raise ValueError(
                "manually_reviewed requires reviewed_by and reviewed_at; "
                "unverified search results cannot carry review data."
            )
        return self
