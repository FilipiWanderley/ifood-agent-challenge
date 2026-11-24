"""Unit tests for the candidate workflow state machine and source evidence."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sabor_da_maria_agent.domain.workflow import (
    ALLOWED_TRANSITIONS,
    CandidateStatus,
    InvalidTransitionError,
    SourceEvidence,
    ensure_transition,
)

S = CandidateStatus

REQUIRED_PATH = [
    (S.DISCOVERED, S.NEEDS_INFORMATION),
    (S.DISCOVERED, S.EVALUATED),
    (S.EVALUATED, S.INFEASIBLE),
    (S.EVALUATED, S.FEASIBLE),
    (S.FEASIBLE, S.AWAITING_APPROVAL),
    (S.AWAITING_APPROVAL, S.APPROVED),
    (S.AWAITING_APPROVAL, S.REJECTED),
    (S.APPROVED, S.COSTED),
    (S.COSTED, S.PRICED),
]


@pytest.mark.parametrize(("source", "target"), REQUIRED_PATH)
def test_required_transitions_are_allowed(source: S, target: S) -> None:
    ensure_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (S.DISCOVERED, S.APPROVED),
        (S.DISCOVERED, S.FEASIBLE),
        (S.NEEDS_INFORMATION, S.FEASIBLE),
        (S.EVALUATED, S.AWAITING_APPROVAL),
        (S.INFEASIBLE, S.APPROVED),
        (S.FEASIBLE, S.APPROVED),
        (S.AWAITING_APPROVAL, S.COSTED),
        (S.APPROVED, S.PRICED),
        (S.REJECTED, S.APPROVED),
        (S.PRICED, S.APPROVED),
        (S.COSTED, S.APPROVED),
    ],
)
def test_invalid_transitions_are_rejected(source: S, target: S) -> None:
    with pytest.raises(InvalidTransitionError, match="Invalid transition"):
        ensure_transition(source, target)


def test_every_status_except_discovered_can_be_invalidated() -> None:
    for status in CandidateStatus:
        if status in (S.DISCOVERED, S.EVALUATED, S.COSTED):
            # DISCOVERED is the target; EVALUATED and COSTED are transient hops
            # inside a single transaction and never persisted as final states.
            continue
        ensure_transition(status, S.DISCOVERED)


def test_transition_table_covers_every_status_and_has_no_self_loops() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(CandidateStatus)
    for status, targets in ALLOWED_TRANSITIONS.items():
        assert status not in targets


def test_costing_is_reachable_only_through_approval() -> None:
    predecessors = {
        source for source, targets in ALLOWED_TRANSITIONS.items() if S.COSTED in targets
    }
    assert predecessors == {S.APPROVED}
    approvers = {
        source
        for source, targets in ALLOWED_TRANSITIONS.items()
        if S.APPROVED in targets
    }
    assert approvers == {S.AWAITING_APPROVAL}


def _evidence(**overrides: object) -> SourceEvidence:
    data: dict[str, object] = {
        "title": "  Galinhada   caipira ",
        "url": "https://receitas.example/galinhada",
        "excerpt": "Arroz,  frango e açafrão.",
        "retrieved_at": datetime(2026, 11, 23, 12, 0, tzinfo=UTC),
        "recorded_at": datetime(2026, 11, 23, 12, 1, tzinfo=UTC),
    }
    data.update(overrides)
    return SourceEvidence.model_validate(data)


def test_source_evidence_is_explicitly_unverified() -> None:
    evidence = _evidence()
    assert evidence.title == "Galinhada caipira"
    assert evidence.excerpt == "Arroz, frango e açafrão."
    assert evidence.discovery_method == "hermes_web_search"
    assert evidence.verification_status == "unverified_search_result"


def test_source_evidence_cannot_claim_verification() -> None:
    with pytest.raises(ValidationError):
        _evidence(verification_status="verified")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x.example/a", "nota"])
def test_source_evidence_requires_http_url(url: str) -> None:
    with pytest.raises(ValidationError):
        _evidence(url=url)


def test_source_evidence_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _evidence(retrieved_at=datetime(2026, 11, 23, 12, 0))
