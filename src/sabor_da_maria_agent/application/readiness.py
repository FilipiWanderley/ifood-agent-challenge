"""Candidate readiness: operational feasibility + pantry, quotes and budget.

This module combines the existing domain services; it introduces no new CMV
or pricing formulas. Purchase totals and budget figures come exclusively from
``RecipeCostCalculator``. The pantry comparison only converts units with
``normalize_measure`` and compares quantities to report what is missing.

Every check uses the SAME ``ProductionPlan`` produced by the feasibility
evaluator: ingredient quantities are the per-batch recipe quantities
multiplied by the plan's whole-batch count, and the cost calculator receives
those same totals with ``servings = total_produced_servings``.
"""

import hashlib
import json
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from sabor_da_maria_agent.domain.costing import (
    CostingError,
    RecipeCostCalculator,
    RecipeIngredientRequirement,
    SupplementalPurchaseQuote,
)
from sabor_da_maria_agent.domain.feasibility import (
    FeasibilityAssessment,
    FeasibilityEvaluator,
    FeasibilityIssue,
    FeasibilityStatus,
)
from sabor_da_maria_agent.domain.kitchen import KitchenProfile
from sabor_da_maria_agent.domain.pantry import IngredientCost, PantryCatalog
from sabor_da_maria_agent.domain.production import (
    ProductionConfirmation,
    ProductionPlan,
)
from sabor_da_maria_agent.domain.recipe import Recipe
from sabor_da_maria_agent.domain.units import normalize_measure

SUPPORTED_UNITS_HINT = "g, kg, ml, l, un"

IngredientCheckStatus = Literal[
    "covered_by_stock",
    "purchase_quoted",
    "purchase_quote_missing",
    "quote_measurement_incompatible",
    "quantity_unknown",
    "pending_accepted",
    "unit_unsupported",
    "measurement_incompatible",
    "production_plan_unknown",
]


class ReadinessModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PantryComparisonLine(ReadinessModel):
    """What the production plan needs versus what the pantry holds.

    Every recipe ingredient has exactly one line, in recipe order, whatever
    its status. ``required_quantity`` / ``base_unit`` refer to the COMPLETE
    production plan in pantry base units; ``original_quantity`` /
    ``original_unit`` are the per-batch values exactly as the recipe stated
    them; ``planned_quantity`` is ``original_quantity * batch_count`` in the
    original unit. Unknown values stay ``None`` (never zero).
    """

    name: str
    required_quantity: Decimal | None
    base_unit: str | None
    in_pantry: bool
    stock_available_quantity: Decimal | None
    shortfall_quantity: Decimal | None
    purchase_quote_provided: bool
    original_quantity: Decimal | None = None
    original_unit: str | None = None
    batch_count: int | None = None
    planned_quantity: Decimal | None = None
    pantry_base_unit: str | None = None
    status: IngredientCheckStatus | None = None
    issue_code: str | None = None


class PurchaseLine(ReadinessModel):
    name: str
    package_count: int
    purchase_total: Decimal
    source: str | None


class PurchasePlan(ReadinessModel):
    """Purchase figures produced by ``RecipeCostCalculator`` (not the LLM)."""

    purchases: tuple[PurchaseLine, ...]
    supplemental_purchase_total: Decimal
    budget_limit: Decimal
    committed_by_other_candidates: Decimal
    available_budget: Decimal
    budget_remaining_after_purchase: Decimal
    within_budget: bool


class CandidateEvaluation(ReadinessModel):
    """Auditable evaluation snapshot persisted with the candidate.

    ``outcome`` is ``feasible`` only when the kitchen can produce the
    complete production plan, every ingredient is quantified in a unit the
    pantry can compare (or explicitly accepted as a pending, partial-CMV
    item), every shortfall has a valid quote and the purchase fits the shared
    remaining budget. ``production_plan`` is the plan every figure refers to.
    """

    outcome: FeasibilityStatus
    operational: FeasibilityAssessment
    pantry: tuple[PantryComparisonLine, ...]
    purchase_plan: PurchasePlan | None
    blockers: tuple[FeasibilityIssue, ...]
    missing_information: tuple[FeasibilityIssue, ...]
    warnings: tuple[str, ...]
    pending_cost_items: tuple[str, ...]
    catalog_fingerprint: str
    evaluated_at: datetime
    production_plan: ProductionPlan | None = None


def normalize_name(value: str) -> str:
    """Name key compatible with the costing engine's catalog matching."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def fingerprint(payload: Any) -> str:
    """SHA-256 of a canonical JSON document."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def catalog_fingerprint(catalog: PantryCatalog) -> str:
    return fingerprint(catalog.model_dump(mode="json"))


def requirements_for(recipe: Recipe) -> list[RecipeIngredientRequirement]:
    """Quantified ingredients of ONE source batch; never invented.

    The official workflow uses ``planned_requirements`` instead, so that
    costing always matches the complete production plan.
    """
    return [
        RecipeIngredientRequirement(
            name=item.name, quantity=item.quantity, unit=item.unit
        )
        for item in recipe.ingredients
        if item.quantity is not None and item.unit is not None
    ]


def planned_requirements(plan: ProductionPlan) -> list[RecipeIngredientRequirement]:
    """Quantified ingredient totals for the complete production plan."""
    return [
        RecipeIngredientRequirement(
            name=item.name, quantity=item.total_quantity, unit=item.unit
        )
        for item in plan.ingredients
        if item.total_quantity is not None and item.unit is not None
    ]


def _issue(code: str, message: str, subject: str | None = None) -> FeasibilityIssue:
    return FeasibilityIssue(code=code, message=message, subject=subject)


@dataclass(frozen=True, slots=True)
class _LineBase:
    """Per-ingredient facts shared by every possible comparison outcome."""

    name: str
    in_pantry: bool
    purchase_quote_provided: bool
    original_quantity: Decimal | None
    original_unit: str | None
    batch_count: int | None
    planned_quantity: Decimal | None
    pantry_base_unit: str | None
    stock_available_quantity: Decimal | None

    def appender(self, lines: list[PantryComparisonLine]) -> Callable[..., None]:
        def add(
            status: IngredientCheckStatus,
            issue_code: str | None = None,
            *,
            required: Decimal | None = None,
            base_unit: str | None = None,
            stock: Decimal | None = None,
            shortfall: Decimal | None = None,
        ) -> None:
            lines.append(
                PantryComparisonLine(
                    name=self.name,
                    required_quantity=required,
                    base_unit=base_unit,
                    in_pantry=self.in_pantry,
                    stock_available_quantity=(
                        stock if stock is not None else self.stock_available_quantity
                    ),
                    shortfall_quantity=shortfall,
                    purchase_quote_provided=self.purchase_quote_provided,
                    original_quantity=self.original_quantity,
                    original_unit=self.original_unit,
                    batch_count=self.batch_count,
                    planned_quantity=self.planned_quantity,
                    pantry_base_unit=self.pantry_base_unit,
                    status=status,
                    issue_code=issue_code,
                )
            )

        return add


def evaluate_readiness(
    *,
    recipe: Recipe,
    kitchen: KitchenProfile | None,
    quotes: Sequence[SupplementalPurchaseQuote],
    allow_partial_cmv: bool,
    catalog: PantryCatalog,
    committed_purchase_total: Decimal,
    evaluated_at: datetime,
    production_confirmation: ProductionConfirmation | None = None,
) -> CandidateEvaluation:
    operational = FeasibilityEvaluator().evaluate(
        recipe, kitchen or KitchenProfile(), production_confirmation
    )
    plan = operational.production_plan
    blockers = list(operational.blockers)
    missing = list(operational.missing_information)
    warnings = list(operational.warnings)
    purchase_issues = False

    pending: tuple[str, ...] = ()
    if recipe.unquantified_ingredients:
        if allow_partial_cmv:
            pending = recipe.unquantified_ingredients
            warnings.append(
                "Accepted as pending (partial CMV): " + ", ".join(pending) + "."
            )
        else:
            missing.extend(
                _issue(
                    "ingredient_quantity_unknown",
                    f"Quantity and unit for '{name}' have not been provided.",
                    name,
                )
                for name in recipe.unquantified_ingredients
            )

    if plan is None:
        purchase_issues = True
        missing.append(
            _issue(
                "production_plan_unknown",
                "Ingredient totals, purchases and budget are only computed for a "
                "complete production plan, which requires the daily target.",
                "production_plan",
            )
        )

    catalog_by_key: dict[str, IngredientCost] = {
        normalize_name(item.name): item for item in catalog.ingredients
    }
    quotes_by_key = {normalize_name(quote.name): quote for quote in quotes}
    planned_by_key = (
        {normalize_name(item.name): item for item in plan.ingredients} if plan else {}
    )
    pantry_lines: list[PantryComparisonLine] = []

    for item in recipe.ingredients:
        key = normalize_name(item.name)
        stock_item = catalog_by_key.get(key)
        quote = quotes_by_key.get(key)
        planned = planned_by_key.get(key)
        stock_quantity = stock_item.stock_base_quantity if stock_item else None
        base = _LineBase(
            name=item.name,
            in_pantry=stock_item is not None,
            purchase_quote_provided=quote is not None,
            original_quantity=item.quantity,
            original_unit=item.unit,
            batch_count=plan.batch_count if plan else None,
            planned_quantity=planned.total_quantity if planned else None,
            pantry_base_unit=stock_item.base_unit if stock_item else None,
            stock_available_quantity=stock_quantity,
        )
        add_line = base.appender(pantry_lines)

        if item.quantity is None or item.unit is None:
            if allow_partial_cmv:
                add_line("pending_accepted")
            else:
                add_line("quantity_unknown", "ingredient_quantity_unknown")
            continue

        try:
            per_batch = normalize_measure(item.quantity, item.unit)
        except ValueError:
            purchase_issues = True
            missing.append(
                _issue(
                    "ingredient_unit_unsupported",
                    f"'{item.name}' is measured as {item.quantity} '{item.unit}' "
                    f"per batch, a unit the pantry cannot compare (supported: "
                    f"{SUPPORTED_UNITS_HINT}). No weight or density is assumed; "
                    "provide a quantity in a supported unit taken from the "
                    "source or confirmed by Dona Maria.",
                    item.name,
                )
            )
            add_line("unit_unsupported", "ingredient_unit_unsupported")
            continue

        if stock_item is not None and (
            stock_item.base_unit != per_batch.unit
            or stock_item.dimension != per_batch.dimension
        ):
            purchase_issues = True
            missing.append(
                _issue(
                    "ingredient_measurement_incompatible",
                    f"'{item.name}' is measured in {per_batch.unit} "
                    f"({per_batch.dimension.value}), but the pantry tracks it in "
                    f"{stock_item.base_unit} ({stock_item.dimension.value}). No "
                    "conversion between them is assumed.",
                    item.name,
                )
            )
            add_line("measurement_incompatible", "ingredient_measurement_incompatible")
            continue

        if plan is None or planned is None or planned.total_quantity is None:
            add_line("production_plan_unknown", "production_plan_unknown")
            continue

        total = normalize_measure(planned.total_quantity, item.unit)
        stock = stock_quantity if stock_quantity is not None else Decimal(0)
        shortfall = max(total.quantity - stock, Decimal(0))
        measured = {
            "required": total.quantity,
            "base_unit": total.unit,
            "stock": stock,
            "shortfall": shortfall,
        }
        if shortfall == 0:
            add_line("covered_by_stock", **measured)
            continue
        if quote is None:
            purchase_issues = True
            missing.append(
                _issue(
                    "purchase_quote_missing",
                    f"'{item.name}' needs {total.quantity} {total.unit} for "
                    f"{plan.batch_count} batch(es); the pantry has {stock}. "
                    f"{shortfall} {total.unit} must be bought; a verifiable "
                    "purchase quote is required.",
                    item.name,
                )
            )
            add_line("purchase_quote_missing", "purchase_quote_missing", **measured)
            continue
        try:
            package = normalize_measure(quote.package_quantity, quote.package_unit)
            compatible = (
                package.unit == total.unit and package.dimension == total.dimension
            )
        except ValueError:
            compatible = False
        if not compatible:
            purchase_issues = True
            missing.append(
                _issue(
                    "quote_measurement_incompatible",
                    f"The quote for '{item.name}' is sold in "
                    f"'{quote.package_unit}', which cannot be compared with the "
                    f"required {total.unit}.",
                    item.name,
                )
            )
            add_line(
                "quote_measurement_incompatible",
                "quote_measurement_incompatible",
                **measured,
            )
            continue
        add_line("purchase_quoted", **measured)

    purchase = None
    if not any(item.quantity is not None for item in recipe.ingredients):
        missing.append(
            _issue(
                "no_quantified_ingredients",
                "At least one quantified ingredient is required for costing.",
            )
        )
    elif plan is not None and not purchase_issues:
        purchase = _purchase_plan(
            recipe,
            plan,
            catalog,
            quotes,
            committed_purchase_total,
            pantry_lines,
            blockers,
            missing,
        )

    if blockers:
        outcome = FeasibilityStatus.INFEASIBLE
    elif missing:
        outcome = FeasibilityStatus.NEEDS_INFORMATION
    else:
        outcome = FeasibilityStatus.FEASIBLE

    return CandidateEvaluation(
        outcome=outcome,
        operational=operational,
        pantry=tuple(pantry_lines),
        purchase_plan=purchase,
        blockers=tuple(blockers),
        missing_information=tuple(missing),
        warnings=tuple(warnings),
        pending_cost_items=pending,
        catalog_fingerprint=catalog_fingerprint(catalog),
        evaluated_at=evaluated_at,
        production_plan=plan,
    )


def _purchase_plan(
    recipe: Recipe,
    plan: ProductionPlan,
    catalog: PantryCatalog,
    quotes: Sequence[SupplementalPurchaseQuote],
    committed_purchase_total: Decimal,
    pantry_lines: Sequence[PantryComparisonLine],
    blockers: list[FeasibilityIssue],
    missing: list[FeasibilityIssue],
) -> PurchasePlan | None:
    try:
        cost = RecipeCostCalculator().calculate(
            recipe_name=recipe.name,
            requirements=planned_requirements(plan),
            catalog=catalog,
            supplemental_quotes=quotes,
            servings=plan.total_produced_servings,
        )
    except CostingError as exc:
        missing.append(_issue("costing_input_invalid", str(exc)))
        return None

    # Invariant: stock checks and purchases describe the same quantities.
    checked = {
        normalize_name(line.name): line.required_quantity for line in pantry_lines
    }
    for line in cost.lines:
        if checked.get(normalize_name(line.name)) != line.required_quantity:
            raise RuntimeError(
                f"Production plan mismatch for {line.name!r}: stock check and "
                "purchase calculation used different quantities."
            )

    available = cost.budget_limit - committed_purchase_total
    within = cost.within_budget and cost.supplemental_purchase_total <= available
    if not within:
        blockers.append(
            _issue(
                "purchase_budget_exceeded",
                f"Supplemental purchases for {plan.batch_count} batch(es) cost "
                f"R$ {cost.supplemental_purchase_total}, but only R$ {available} "
                f"of the shared R$ {cost.budget_limit} budget remains.",
                "remaining_budget",
            )
        )
    return PurchasePlan(
        purchases=tuple(
            PurchaseLine(
                name=line.name,
                package_count=line.purchase_package_count,
                purchase_total=line.purchase_total,
                source=line.purchase_source,
            )
            for line in cost.lines
            if line.purchase_package_count > 0
        ),
        supplemental_purchase_total=cost.supplemental_purchase_total,
        budget_limit=cost.budget_limit,
        committed_by_other_candidates=committed_purchase_total,
        available_budget=available,
        budget_remaining_after_purchase=available - cost.supplemental_purchase_total,
        within_budget=within,
    )
