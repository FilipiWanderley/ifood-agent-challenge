"""Official conversational planning workflow (application layer).

Orchestrates the recipe/kitchen/feasibility domain, the readiness evaluation
and the existing financial calculators over a transactional repository.
Business rules live here and in the domain, never in MCP tools or prompts.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, TypeAdapter

from sabor_da_maria_agent.application.readiness import (
    CandidateEvaluation,
    catalog_fingerprint,
    evaluate_readiness,
    fingerprint,
    normalize_name,
    planned_requirements,
)
from sabor_da_maria_agent.domain.costing import (
    DeliveryPricingCalculator,
    RecipeCostCalculator,
    SupplementalPurchaseQuote,
)
from sabor_da_maria_agent.domain.feasibility import FeasibilityStatus
from sabor_da_maria_agent.domain.kitchen import KitchenProfile
from sabor_da_maria_agent.domain.pantry import PantryCatalog
from sabor_da_maria_agent.domain.production import (
    ProductionConfirmation,
    required_batches,
)
from sabor_da_maria_agent.domain.recipe import Recipe, RecipeSource
from sabor_da_maria_agent.domain.units import normalize_measure
from sabor_da_maria_agent.domain.workflow import (
    BUDGET_COMMITTED_STATUSES,
    CandidateStatus,
    SourceEvidence,
    ensure_transition,
)

MAX_SOURCES = 10
MAX_QUOTES = 50
MAX_CONFIRMATION_LENGTH = 2000
_ID_LENGTH = 32


class PlanningError(ValueError):
    """Base class for deterministic workflow rejections."""


class NotFoundError(PlanningError):
    pass


class StaleVersionError(PlanningError):
    pass


class ApprovalError(PlanningError):
    pass


class BudgetExceededError(PlanningError):
    pass


class ApprovalRecord(BaseModel):
    """Explicit user decision bound to an exact candidate version and inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["approved", "rejected"]
    candidate_version: int = Field(gt=0)
    input_fingerprint: str = Field(min_length=64, max_length=64)
    user_confirmation: str = Field(min_length=1, max_length=MAX_CONFIRMATION_LENGTH)
    supplemental_purchase_total: Decimal | None
    decided_at: datetime
    production_batch_count: int | None = Field(default=None, gt=0)
    total_produced_servings: int | None = Field(default=None, gt=0)


@dataclass(frozen=True, slots=True)
class PlanningSession:
    session_id: str
    version: int
    kitchen_profile: KitchenProfile | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecipeCandidate:
    candidate_id: str
    session_id: str
    version: int
    status: CandidateStatus
    recipe: Recipe
    evidence: tuple[SourceEvidence, ...]
    quotes: tuple[SupplementalPurchaseQuote, ...]
    allow_partial_cmv: bool
    production_confirmation: ProductionConfirmation | None
    evaluation: CandidateEvaluation | None
    input_fingerprint: str | None
    approval: ApprovalRecord | None
    calculation: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    session_id: str
    candidate_id: str | None
    event_type: str
    from_status: CandidateStatus | None
    to_status: CandidateStatus | None
    version: int
    payload: dict[str, Any]
    created_at: datetime


class PlanningUnitOfWork(Protocol):
    def get_session(self, session_id: str) -> PlanningSession | None: ...
    def insert_session(self, session: PlanningSession) -> None: ...
    def update_session(
        self, session: PlanningSession, expected_version: int
    ) -> None: ...
    def get_candidate(self, candidate_id: str) -> RecipeCandidate | None: ...
    def list_candidates(self, session_id: str) -> list[RecipeCandidate]: ...
    def insert_candidate(self, candidate: RecipeCandidate) -> None: ...
    def update_candidate(
        self, candidate: RecipeCandidate, expected_version: int
    ) -> None: ...
    def add_event(self, event: WorkflowEvent) -> None: ...
    def approval_sequence(self, candidate_id: str) -> int | None: ...
    def list_events(
        self, session_id: str, candidate_id: str | None, limit: int
    ) -> list[WorkflowEvent]: ...


class PlanningRepository(Protocol):
    def transaction(self) -> AbstractContextManager[PlanningUnitOfWork]: ...


class _ManualReviewInput(BaseModel):
    """A human review attestation; recorded as reported, never automated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    note: str | None = Field(default=None, max_length=1000)


class _SourceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    url: AnyHttpUrl
    excerpt: str | None = Field(default=None, max_length=2000)
    retrieved_at: datetime | None = None
    publisher: str | None = Field(default=None, min_length=1, max_length=200)
    manual_review: _ManualReviewInput | None = None


_HTTP_URL = TypeAdapter(AnyHttpUrl)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


def _validate_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _ID_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise PlanningError(f"{label} must be a 32-character lowercase hex id.")
    return value


def _validate_version(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanningError(f"{label} must be a positive integer.")
    return value


def _check_version(actual: int, expected: int, label: str) -> None:
    if actual != expected:
        raise StaleVersionError(
            f"Stale {label} version: expected {expected}, current is {actual}. "
            "Reload the planning status before retrying."
        )


class PlanningService:
    """Deterministic use cases of the official recipe-planning workflow."""

    def __init__(
        self,
        repository: PlanningRepository,
        catalog_provider: Callable[[], PantryCatalog],
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_id,
    ) -> None:
        self._repository = repository
        self._catalog_provider = catalog_provider
        self._clock = clock
        self._id_factory = id_factory

    # ------------------------------------------------------------------ session
    def start_session(
        self, kitchen_profile: Mapping[str, Any] | None = None
    ) -> PlanningSession:
        profile = (
            KitchenProfile.model_validate(dict(kitchen_profile))
            if kitchen_profile is not None
            else None
        )
        now = self._clock()
        session = PlanningSession(
            session_id=self._id_factory(),
            version=1,
            kitchen_profile=profile,
            created_at=now,
            updated_at=now,
        )
        with self._repository.transaction() as uow:
            uow.insert_session(session)
            uow.add_event(
                WorkflowEvent(
                    session.session_id, None, "session_started", None, None, 1, {}, now
                )
            )
        return session

    def update_kitchen_profile(
        self,
        session_id: str,
        expected_session_version: int,
        updates: Mapping[str, Any],
    ) -> tuple[PlanningSession, list[str]]:
        """Merge kitchen facts; explicit ``null`` resets a fact to unknown.

        Every candidate of the session is invalidated (back to DISCOVERED),
        because feasibility and approval depended on the previous profile.
        """
        _validate_id(session_id, "session_id")
        _validate_version(expected_session_version, "expected_session_version")
        if not isinstance(updates, Mapping) or not updates:
            raise PlanningError("kitchen_profile updates must be a non-empty object.")

        with self._repository.transaction() as uow:
            session = self._require_session(uow, session_id)
            _check_version(session.version, expected_session_version, "session")
            base = (
                session.kitchen_profile.model_dump(mode="json")
                if session.kitchen_profile is not None
                else {}
            )
            profile = KitchenProfile.model_validate({**base, **dict(updates)})
            now = self._clock()
            updated = replace(
                session,
                version=session.version + 1,
                kitchen_profile=profile,
                updated_at=now,
            )
            uow.update_session(updated, session.version)
            uow.add_event(
                WorkflowEvent(
                    session_id,
                    None,
                    "kitchen_profile_updated",
                    None,
                    None,
                    updated.version,
                    {"fields": sorted(updates)},
                    now,
                )
            )
            invalidated = [
                candidate.candidate_id
                for candidate in uow.list_candidates(session_id)
                if self._invalidate(uow, candidate, "kitchen_profile_changed", now)
            ]
        return updated, invalidated

    # --------------------------------------------------------------- candidates
    def register_candidate(
        self,
        session_id: str,
        recipe: Mapping[str, Any],
        sources: Sequence[Mapping[str, Any]],
        supplemental_quotes: Sequence[Mapping[str, Any]] | None = None,
        *,
        allow_partial_cmv: bool = False,
        candidate_id: str | None = None,
        expected_candidate_version: int | None = None,
        production_confirmation: Mapping[str, Any] | None = None,
    ) -> RecipeCandidate:
        """Register a researched candidate, or revise an existing one.

        A revision replaces recipe, sources, quotes and the production
        confirmation and invalidates any previous evaluation, approval and
        calculation.
        """
        _validate_id(session_id, "session_id")
        if not isinstance(allow_partial_cmv, bool):
            raise PlanningError("allow_partial_cmv must be a boolean.")
        if production_confirmation is not None and not isinstance(
            production_confirmation, Mapping
        ):
            raise PlanningError("production_confirmation must be an object.")
        confirmation = (
            ProductionConfirmation.model_validate(dict(production_confirmation))
            if production_confirmation is not None
            else None
        )
        now = self._clock()
        parsed_recipe, evidence = self._parse_recipe(recipe, sources, now)
        quotes = self._parse_quotes(supplemental_quotes or (), parsed_recipe)

        with self._repository.transaction() as uow:
            session = self._require_session(uow, session_id)
            # Bind the human's confirmation to the exact source yield AND
            # daily target recorded when she confirmed the plan. Merely
            # matching the batch count is insufficient (e.g. 9 vs 15 target).
            if confirmation is not None:
                target = (
                    session.kitchen_profile.target_servings_per_day
                    if session.kitchen_profile is not None
                    else None
                )
                if target is None:
                    raise PlanningError(
                        "Set target_servings_per_day before recording a production "
                        "confirmation."
                    )
                if confirmation.confirmed_batch_count != required_batches(
                    target, parsed_recipe.servings
                ):
                    raise PlanningError(
                        "The confirmed batch count does not match the current "
                        "recipe yield and daily target. Reconfirm the plan."
                    )
                confirmation = confirmation.model_copy(
                    update={
                        "confirmed_target_daily_servings": target,
                        "confirmed_source_recipe_yield": parsed_recipe.servings,
                    }
                )
            siblings = uow.list_candidates(session_id)

            if candidate_id is None:
                if expected_candidate_version is not None:
                    raise PlanningError(
                        "expected_candidate_version requires candidate_id."
                    )
                if any(c.recipe.recipe_id == parsed_recipe.recipe_id for c in siblings):
                    raise PlanningError(
                        f"recipe_id {parsed_recipe.recipe_id!r} is already registered "
                        "in this session; pass candidate_id to revise it."
                    )
                candidate = RecipeCandidate(
                    candidate_id=self._id_factory(),
                    session_id=session_id,
                    version=1,
                    status=CandidateStatus.DISCOVERED,
                    recipe=parsed_recipe,
                    evidence=evidence,
                    quotes=quotes,
                    allow_partial_cmv=allow_partial_cmv,
                    production_confirmation=confirmation,
                    evaluation=None,
                    input_fingerprint=None,
                    approval=None,
                    calculation=None,
                    created_at=now,
                    updated_at=now,
                )
                uow.insert_candidate(candidate)
                uow.add_event(
                    WorkflowEvent(
                        session_id,
                        candidate.candidate_id,
                        "candidate_registered",
                        None,
                        CandidateStatus.DISCOVERED,
                        1,
                        {"recipe_id": parsed_recipe.recipe_id},
                        now,
                    )
                )
                return candidate

            _validate_id(candidate_id, "candidate_id")
            if expected_candidate_version is None:
                raise PlanningError(
                    "Revising a candidate requires its expected version."
                )
            _validate_version(expected_candidate_version, "expected_candidate_version")
            current = self._require_candidate(uow, session_id, candidate_id)
            _check_version(current.version, expected_candidate_version, "candidate")
            if any(
                c.recipe.recipe_id == parsed_recipe.recipe_id
                and c.candidate_id != candidate_id
                for c in siblings
            ):
                raise PlanningError(
                    f"recipe_id {parsed_recipe.recipe_id!r} belongs to another candidate."
                )
            status = current.status
            if status != CandidateStatus.DISCOVERED:
                ensure_transition(status, CandidateStatus.DISCOVERED)
            revised = replace(
                current,
                version=current.version + 1,
                status=CandidateStatus.DISCOVERED,
                recipe=parsed_recipe,
                evidence=evidence,
                quotes=quotes,
                allow_partial_cmv=allow_partial_cmv,
                production_confirmation=confirmation,
                evaluation=None,
                input_fingerprint=None,
                approval=None,
                calculation=None,
                updated_at=now,
            )
            uow.update_candidate(revised, current.version)
            uow.add_event(
                WorkflowEvent(
                    session_id,
                    candidate_id,
                    "candidate_revised",
                    status,
                    CandidateStatus.DISCOVERED,
                    revised.version,
                    {"invalidated_approval": current.approval is not None},
                    now,
                )
            )
            return revised

    def evaluate_candidate(
        self, session_id: str, candidate_id: str, expected_candidate_version: int
    ) -> RecipeCandidate:
        """Run feasibility + pantry + quote + budget checks deterministically."""
        self._validate_candidate_ref(
            session_id, candidate_id, expected_candidate_version
        )
        catalog = self._catalog_provider()
        with self._repository.transaction() as uow:
            session = self._require_session(uow, session_id)
            current = self._require_candidate(uow, session_id, candidate_id)
            _check_version(current.version, expected_candidate_version, "candidate")
            # Reserve real pantry quantities consumed by other approved
            # recipes of the SAME session. Purchased packages do not magically
            # replenish the workbook; they remain per-recipe costs.
            available_catalog = self._available_catalog(uow, current, catalog)

            path: list[CandidateStatus] = []
            status = current.status
            if status in (
                CandidateStatus.NEEDS_INFORMATION,
                CandidateStatus.INFEASIBLE,
            ):
                path.append(CandidateStatus.DISCOVERED)
            elif status != CandidateStatus.DISCOVERED:
                ensure_transition(status, CandidateStatus.EVALUATED)

            now = self._clock()
            evaluation = evaluate_readiness(
                recipe=current.recipe,
                kitchen=session.kitchen_profile,
                quotes=current.quotes,
                allow_partial_cmv=current.allow_partial_cmv,
                catalog=available_catalog,
                committed_purchase_total=self._committed_total(uow, current),
                evaluated_at=now,
                production_confirmation=current.production_confirmation,
            )
            if evaluation.outcome == FeasibilityStatus.NEEDS_INFORMATION:
                path.append(CandidateStatus.NEEDS_INFORMATION)
            elif evaluation.outcome == FeasibilityStatus.INFEASIBLE:
                path += [CandidateStatus.EVALUATED, CandidateStatus.INFEASIBLE]
            else:
                path += [
                    CandidateStatus.EVALUATED,
                    CandidateStatus.FEASIBLE,
                    CandidateStatus.AWAITING_APPROVAL,
                ]

            return self._advance(
                uow,
                current,
                path,
                now,
                "candidate_evaluated",
                {
                    "outcome": evaluation.outcome.value,
                    "blockers": [issue.code for issue in evaluation.blockers],
                    "missing_information": [
                        issue.code for issue in evaluation.missing_information
                    ],
                },
                evaluation=evaluation,
                input_fingerprint=self._input_fingerprint(
                    current, session.kitchen_profile, evaluation.catalog_fingerprint
                ),
                approval=None,
                calculation=None,
            )

    def decide_candidate(
        self,
        session_id: str,
        candidate_id: str,
        expected_candidate_version: int,
        decision: str,
        user_confirmation: str,
    ) -> RecipeCandidate:
        """Record Dona Maria's explicit approval or rejection.

        Approval binds the exact candidate version and input fingerprint.
        Repeating the same decision for the same version is idempotent.
        """
        self._validate_candidate_ref(
            session_id, candidate_id, expected_candidate_version
        )
        if decision not in ("approve", "reject"):
            raise PlanningError("decision must be 'approve' or 'reject'.")
        if not isinstance(user_confirmation, str) or not user_confirmation.strip():
            raise PlanningError(
                "user_confirmation must quote Dona Maria's explicit decision."
            )
        if len(user_confirmation) > MAX_CONFIRMATION_LENGTH:
            raise PlanningError("user_confirmation is too long.")
        recorded: Literal["approved", "rejected"] = (
            "approved" if decision == "approve" else "rejected"
        )
        target = (
            CandidateStatus.APPROVED
            if decision == "approve"
            else CandidateStatus.REJECTED
        )

        budget_message: str | None = None
        with self._repository.transaction() as uow:
            current = self._require_candidate(uow, session_id, candidate_id)
            approval = current.approval
            if (
                approval is not None
                and approval.decision == recorded
                and approval.candidate_version == expected_candidate_version
                and current.version == expected_candidate_version + 1
            ):
                return current  # idempotent repeat of the same decision
            _check_version(current.version, expected_candidate_version, "candidate")
            ensure_transition(current.status, target)
            evaluation = current.evaluation
            if (
                evaluation is None
                or evaluation.outcome != FeasibilityStatus.FEASIBLE
                or current.input_fingerprint is None
            ):
                raise ApprovalError("Candidate has no feasible evaluation to approve.")
            plan = evaluation.production_plan
            if plan is None:
                raise ApprovalError(
                    "Candidate evaluation has no production plan; re-evaluate it."
                )

            purchase_total = (
                evaluation.purchase_plan.supplemental_purchase_total
                if evaluation.purchase_plan is not None
                else None
            )
            if target == CandidateStatus.APPROVED and evaluation.purchase_plan:
                purchase = evaluation.purchase_plan
                available = purchase.budget_limit - self._committed_total(uow, current)
                if purchase.supplemental_purchase_total > available:
                    budget_message = (
                        "Approval would spend R$ "
                        f"{purchase.supplemental_purchase_total}, "
                        f"but only R$ {available} of the shared budget remains. "
                        "The old evaluation has been invalidated; re-evaluate."
                    )

            now = self._clock()
            if target == CandidateStatus.APPROVED and budget_message is None:
                current_catalog = self._available_catalog(
                    uow, current, self._catalog_provider()
                )
                # Prevent approval after the Excel stock/price changes or
                # another candidate reserves pantry inventory. Commit the
                # invalidation before returning the user-facing error.
                snapshot = catalog_fingerprint(current_catalog)
                current_fingerprint = self._input_fingerprint(
                    current,
                    self._require_session(uow, session_id).kitchen_profile,
                    snapshot,
                )
                stale = (
                    snapshot != evaluation.catalog_fingerprint
                    or current_fingerprint != current.input_fingerprint
                )
            else:
                stale = False
            if stale or budget_message is not None:
                invalidated = self._advance(
                    uow,
                    current,
                    [CandidateStatus.DISCOVERED],
                    now,
                    "approval_invalidated",
                    {
                        "reason": (
                            "shared_budget_committed_since_evaluation"
                            if budget_message is not None
                            else "pantry_or_reservations_changed_since_evaluation"
                        )
                    },
                    evaluation=None,
                    input_fingerprint=None,
                    approval=None,
                    calculation=None,
                )
            else:
                record = ApprovalRecord(
                    decision=recorded,
                    candidate_version=current.version,
                    input_fingerprint=current.input_fingerprint,
                    user_confirmation=" ".join(user_confirmation.split()),
                    supplemental_purchase_total=purchase_total,
                    decided_at=now,
                    production_batch_count=plan.batch_count,
                    total_produced_servings=plan.total_produced_servings,
                )
                return self._advance(
                    uow,
                    current,
                    [target],
                    now,
                    f"candidate_{recorded}",
                    {"approved_version": current.version},
                    approval=record,
                )
        if budget_message is not None:
            raise BudgetExceededError(
                f"{budget_message} Candidate reset to discovered "
                f"(version {invalidated.version})."
            )
        raise ApprovalError(
            "Pantry or other approved recipes changed since evaluation. "
            f"Candidate reset to discovered (version {invalidated.version}). "
            "Re-evaluate and ask Dona Maria to approve the new plan."
        )

    def calculate_approved(
        self,
        session_id: str,
        candidate_id: str,
        expected_candidate_version: int,
        *,
        include_batch_pricing: bool = False,
    ) -> RecipeCandidate:
        """Compute CMV and delivery pricing for an explicitly approved candidate.

        Reuses ``RecipeCostCalculator`` and ``DeliveryPricingCalculator``.
        A repeated call on an already priced candidate returns the stored
        result without recalculating.
        """
        self._validate_candidate_ref(
            session_id, candidate_id, expected_candidate_version
        )
        if not isinstance(include_batch_pricing, bool):
            raise PlanningError("include_batch_pricing must be a boolean.")
        catalog = self._catalog_provider()

        with self._repository.transaction() as uow:
            session = self._require_session(uow, session_id)
            current = self._require_candidate(uow, session_id, candidate_id)
            if (
                current.status == CandidateStatus.PRICED
                and current.calculation is not None
                and current.version == expected_candidate_version + 1
            ):
                return current  # idempotent repeat
            _check_version(current.version, expected_candidate_version, "candidate")
            if current.status != CandidateStatus.APPROVED:
                raise ApprovalError(
                    f"Candidate status is {current.status.value!r}; CMV and pricing "
                    "require an explicit approval (status 'approved')."
                )
            approval = current.approval
            evaluation = current.evaluation
            if (
                approval is None
                or approval.decision != "approved"
                or evaluation is None
            ):
                raise ApprovalError(
                    "No explicit approval is recorded for this candidate."
                )
            if approval.candidate_version != current.version - 1:
                raise ApprovalError(
                    "Approval does not match the current candidate version."
                )

            now = self._clock()
            available_catalog = self._available_catalog(uow, current, catalog)
            fresh = self._input_fingerprint(
                current,
                session.kitchen_profile,
                catalog_fingerprint(available_catalog),
            )
            if (
                fresh != approval.input_fingerprint
                or fresh != current.input_fingerprint
            ):
                # Commit the invalidation, then reject outside the transaction.
                invalidated = self._advance(
                    uow,
                    current,
                    [CandidateStatus.DISCOVERED],
                    now,
                    "approval_invalidated",
                    {"reason": "inputs_or_pantry_changed_since_approval"},
                    evaluation=None,
                    input_fingerprint=None,
                    approval=None,
                    calculation=None,
                )
            else:
                return self._price(
                    uow, current, available_catalog, now, include_batch_pricing
                )
        raise ApprovalError(
            "Recipe, kitchen, quotes or pantry changed since approval. "
            f"Candidate reset to 'discovered' (version {invalidated.version}); "
            "re-evaluate and obtain a new approval."
        )

    def _price(
        self,
        uow: PlanningUnitOfWork,
        current: RecipeCandidate,
        catalog: PantryCatalog,
        now: datetime,
        include_batch_pricing: bool,
    ) -> RecipeCandidate:
        approval = current.approval
        evaluation = current.evaluation
        if approval is None or evaluation is None:
            raise ApprovalError("No explicit approval is recorded for this candidate.")
        plan = evaluation.production_plan
        if plan is None or (
            approval.production_batch_count is not None
            and approval.production_batch_count != plan.batch_count
        ):
            raise ApprovalError(
                "The approved production plan is missing or differs; re-evaluate."
            )
        # Same plan as the stock check, purchases and budget approval.
        recipe_cost = RecipeCostCalculator().calculate(
            recipe_name=current.recipe.name,
            requirements=planned_requirements(plan),
            catalog=catalog,
            supplemental_quotes=current.quotes,
            servings=plan.total_produced_servings,
        )
        if (
            recipe_cost.supplemental_purchase_total
            != approval.supplemental_purchase_total
        ):
            raise ApprovalError("Purchase total differs from the approved amount.")
        report = DeliveryPricingCalculator().calculate(
            recipe_cost,
            include_batch=include_batch_pricing,
            pending_cost_items=evaluation.pending_cost_items,
        )
        calculation: dict[str, Any] = {
            "recipe_cost": recipe_cost.model_dump(mode="json"),
            "delivery_pricing": report.model_dump(mode="json"),
            "servings": report.servings,
            "production_plan": plan.model_dump(mode="json"),
            "cmv_scope": "complete_production_plan",
            "total_cmv_brl": str(report.total_cmv),
            "cmv_per_portion_brl": str(report.cmv_per_portion),
            "break_even_price_per_portion_brl": str(
                report.per_portion.break_even_price
            ),
            "cmv_complete": report.cmv_complete,
            "pending_cost_items": list(report.pending_cost_items),
            "supplemental_purchase_total_brl": str(
                recipe_cost.supplemental_purchase_total
            ),
            "approved_candidate_version": approval.candidate_version,
            "excluded_costs": [
                "packaging",
                "gas",
                "electricity",
                "labour",
                "taxes",
            ],
            "calculated_at": now.isoformat(),
            "currency": "BRL",
        }
        return self._advance(
            uow,
            current,
            [CandidateStatus.COSTED, CandidateStatus.PRICED],
            now,
            "candidate_priced",
            {
                "total_cmv_brl": calculation["total_cmv_brl"],
                "cmv_complete": report.cmv_complete,
            },
            calculation=calculation,
        )

    # ------------------------------------------------------------------- status
    def get_status(
        self, session_id: str, candidate_id: str | None = None, event_limit: int = 50
    ) -> tuple[PlanningSession, list[RecipeCandidate], list[WorkflowEvent]]:
        _validate_id(session_id, "session_id")
        if candidate_id is not None:
            _validate_id(candidate_id, "candidate_id")
        with self._repository.transaction() as uow:
            session = self._require_session(uow, session_id)
            if candidate_id is None:
                candidates = uow.list_candidates(session_id)
            else:
                candidates = [self._require_candidate(uow, session_id, candidate_id)]
            events = uow.list_events(session_id, candidate_id, event_limit)
        return session, candidates, events

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _require_session(uow: PlanningUnitOfWork, session_id: str) -> PlanningSession:
        session = uow.get_session(session_id)
        if session is None:
            raise NotFoundError(f"Planning session {session_id!r} not found.")
        return session

    @staticmethod
    def _require_candidate(
        uow: PlanningUnitOfWork, session_id: str, candidate_id: str
    ) -> RecipeCandidate:
        candidate = uow.get_candidate(candidate_id)
        if candidate is None or candidate.session_id != session_id:
            raise NotFoundError(
                f"Candidate {candidate_id!r} not found in session {session_id!r}."
            )
        return candidate

    @staticmethod
    def _validate_candidate_ref(
        session_id: str, candidate_id: str, expected_version: int
    ) -> None:
        _validate_id(session_id, "session_id")
        _validate_id(candidate_id, "candidate_id")
        _validate_version(expected_version, "expected_candidate_version")

    @staticmethod
    def _available_catalog(
        uow: PlanningUnitOfWork,
        current: RecipeCandidate,
        catalog: PantryCatalog,
    ) -> PantryCatalog:
        """Available stock after earlier committed recipes in this session.

        A candidate not yet approved sees ALL committed consumption. An
        approved candidate sees only PRIOR approvals, preserving its stock
        allocation if another recipe is approved later. Transactions serialize
        approvals, and immutable event IDs define the allocation order.
        Quantities purchased for a candidate are NOT treated as shared stock.
        """
        committed = [
            other
            for other in uow.list_candidates(current.session_id)
            if other.candidate_id != current.candidate_id
            and other.status in BUDGET_COMMITTED_STATUSES
            and other.approval is not None
        ]
        # Order by durable, transactional event_id, not wall-clock timestamps.
        # Distinct approvals can share the same clock tick in unit tests or
        # after a VM clock adjustment.
        cutoff = (
            uow.approval_sequence(current.candidate_id)
            if current.approval is not None
            and current.status in BUDGET_COMMITTED_STATUSES
            else None
        )
        if (
            current.approval is not None
            and current.status in BUDGET_COMMITTED_STATUSES
            and cutoff is None
        ):
            raise ApprovalError(
                f"Approved recipe {current.candidate_id} lacks its approval event."
            )
        ranked: list[tuple[int, RecipeCandidate]] = []
        for other in committed:
            sequence = uow.approval_sequence(other.candidate_id)
            if sequence is None:
                raise ApprovalError(
                    f"Committed recipe {other.candidate_id} lacks its approval event."
                )
            if cutoff is None or sequence < cutoff:
                ranked.append((sequence, other))
        ranked.sort(key=lambda row: row[0])
        committed = [other for _, other in ranked]
        used: dict[str, Decimal] = {}
        known = {normalize_name(item.name): item for item in catalog.ingredients}
        for other in committed:
            plan = other.evaluation.production_plan if other.evaluation else None
            if plan is None:
                raise ApprovalError("Committed recipe is missing its production plan.")
            for requirement in planned_requirements(plan):
                key = normalize_name(requirement.name)
                original = known.get(key)
                if original is None:
                    continue  # an ingredient absent from the source pantry
                measure = normalize_measure(requirement.quantity, requirement.unit)
                if (
                    original.base_unit != measure.unit
                    or original.dimension != measure.dimension
                ):
                    raise ApprovalError(
                        f"Reserved ingredient {requirement.name!r} has changed units."
                    )
                used[key] = used.get(key, Decimal(0)) + measure.quantity
        available = []
        for item in catalog.ingredients:
            consumed = used.get(normalize_name(item.name), Decimal(0))
            remaining = max(Decimal(0), item.stock_base_quantity - consumed)
            # Keep the source display unit in sync with the normalized unit.
            display_remaining = (
                item.stock_quantity * remaining / item.stock_base_quantity
                if item.stock_base_quantity > 0
                else Decimal(0)
            )
            available.append(
                item.model_copy(
                    update={
                        "stock_base_quantity": remaining,
                        "stock_quantity": display_remaining,
                    }
                )
            )
        return catalog.model_copy(update={"ingredients": tuple(available)})

    @staticmethod
    def _committed_total(uow: PlanningUnitOfWork, current: RecipeCandidate) -> Decimal:
        """Purchases already approved for *other* candidates of the session."""
        total = Decimal(0)
        for other in uow.list_candidates(current.session_id):
            if (
                other.candidate_id != current.candidate_id
                and other.status in BUDGET_COMMITTED_STATUSES
                and other.approval is not None
                and other.approval.supplemental_purchase_total is not None
            ):
                total += other.approval.supplemental_purchase_total
        return total

    @staticmethod
    def _input_fingerprint(
        candidate: RecipeCandidate,
        kitchen: KitchenProfile | None,
        catalog_digest: str,
    ) -> str:
        return fingerprint(
            {
                "recipe": candidate.recipe.model_dump(mode="json"),
                "quotes": [quote.model_dump(mode="json") for quote in candidate.quotes],
                "allow_partial_cmv": candidate.allow_partial_cmv,
                "production_confirmation": (
                    candidate.production_confirmation.model_dump(mode="json")
                    if candidate.production_confirmation is not None
                    else None
                ),
                "kitchen": kitchen.model_dump(mode="json") if kitchen else None,
                "catalog": catalog_digest,
            }
        )

    def _advance(
        self,
        uow: PlanningUnitOfWork,
        current: RecipeCandidate,
        path: Sequence[CandidateStatus],
        now: datetime,
        event_type: str,
        payload: dict[str, Any],
        **changes: Any,
    ) -> RecipeCandidate:
        """Validate every hop, persist once, and record one event per hop."""
        status = current.status
        hops: list[tuple[CandidateStatus, CandidateStatus]] = []
        for target in path:
            ensure_transition(status, target)
            hops.append((status, target))
            status = target
        updated = replace(
            current,
            version=current.version + 1,
            status=status,
            updated_at=now,
            **changes,
        )
        uow.update_candidate(updated, current.version)
        for index, (source, target) in enumerate(hops):
            uow.add_event(
                WorkflowEvent(
                    current.session_id,
                    current.candidate_id,
                    event_type if index == len(hops) - 1 else "status_changed",
                    source,
                    target,
                    updated.version,
                    payload if index == len(hops) - 1 else {},
                    now,
                )
            )
        return updated

    def _invalidate(
        self,
        uow: PlanningUnitOfWork,
        candidate: RecipeCandidate,
        reason: str,
        now: datetime,
    ) -> bool:
        if candidate.status == CandidateStatus.DISCOVERED:
            return False
        self._advance(
            uow,
            candidate,
            [CandidateStatus.DISCOVERED],
            now,
            "candidate_invalidated",
            {"reason": reason},
            evaluation=None,
            input_fingerprint=None,
            approval=None,
            calculation=None,
        )
        return True

    @staticmethod
    def _parse_recipe(
        recipe: Mapping[str, Any],
        sources: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> tuple[Recipe, tuple[SourceEvidence, ...]]:
        if not isinstance(recipe, Mapping):
            raise PlanningError("recipe must be an object.")
        if "sources" in recipe:
            raise PlanningError(
                "Pass recipe sources in the separate 'sources' argument "
                "(title, url, excerpt, retrieved_at)."
            )
        if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
            raise PlanningError("sources must be a list of web_search results.")
        if not 1 <= len(sources) <= MAX_SOURCES:
            raise PlanningError(f"Provide between 1 and {MAX_SOURCES} sources.")
        parsed_sources = [_SourceInput.model_validate(dict(item)) for item in sources]
        urls = [str(item.url) for item in parsed_sources]
        if len(set(urls)) != len(urls):
            raise PlanningError("Source URLs must be unique.")

        parsed = Recipe.model_validate(
            {
                **dict(recipe),
                "sources": [
                    RecipeSource(title=s.title, url=s.url, publisher=s.publisher)
                    for s in parsed_sources
                ],
            }
        )
        names = [normalize_name(item.name) for item in parsed.ingredients]
        if len(set(names)) != len(names):
            raise PlanningError("Recipe ingredient names must be unique.")
        evidence = tuple(
            SourceEvidence(
                title=s.title,
                url=s.url,
                excerpt=s.excerpt,
                retrieved_at=s.retrieved_at,
                recorded_at=now,
                verification_status=(
                    "manually_reviewed"
                    if s.manual_review is not None
                    else "unverified_search_result"
                ),
                reviewed_by=s.manual_review.reviewed_by if s.manual_review else None,
                reviewed_at=s.manual_review.reviewed_at if s.manual_review else None,
                review_note=s.manual_review.note if s.manual_review else None,
            )
            for s in parsed_sources
        )
        return parsed, evidence

    @staticmethod
    def _parse_quotes(
        quotes: Sequence[Mapping[str, Any]], recipe: Recipe
    ) -> tuple[SupplementalPurchaseQuote, ...]:
        if isinstance(quotes, (str, bytes)) or not isinstance(quotes, Sequence):
            raise PlanningError("supplemental_quotes must be a list.")
        if len(quotes) > MAX_QUOTES:
            raise PlanningError(
                f"At most {MAX_QUOTES} supplemental quotes are allowed."
            )
        ingredient_keys = {normalize_name(item.name) for item in recipe.ingredients}
        parsed: list[SupplementalPurchaseQuote] = []
        seen: set[str] = set()
        for index, item in enumerate(quotes, start=1):
            if not isinstance(item, Mapping):
                raise PlanningError(f"Quote at position {index} must be an object.")
            if any(isinstance(value, float) for value in item.values()):
                raise PlanningError(
                    f"Quote at position {index}: send numeric values as strings."
                )
            quote = SupplementalPurchaseQuote.model_validate(dict(item))
            _HTTP_URL.validate_python(quote.source)
            key = normalize_name(quote.name)
            if key not in ingredient_keys:
                raise PlanningError(
                    f"Quote {quote.name!r} does not match any recipe ingredient."
                )
            if key in seen:
                raise PlanningError(f"Duplicate quote for {quote.name!r}.")
            seen.add(key)
            parsed.append(quote)
        return tuple(parsed)
