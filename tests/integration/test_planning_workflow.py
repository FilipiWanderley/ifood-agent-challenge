"""Integration tests: PlanningService over a temporary SQLite database."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sabor_da_maria_agent.application.planning import (
    ApprovalError,
    BudgetExceededError,
    NotFoundError,
    PlanningError,
    PlanningService,
    RecipeCandidate,
    StaleVersionError,
)
from sabor_da_maria_agent.application.readiness import requirements_for
from sabor_da_maria_agent.domain.costing import (
    DeliveryPricingCalculator,
    RecipeCostCalculator,
    SupplementalPurchaseQuote,
)
from sabor_da_maria_agent.domain.pantry import IngredientCost, PantryCatalog
from sabor_da_maria_agent.domain.units import MeasureDimension
from sabor_da_maria_agent.domain.workflow import (
    CandidateStatus,
    InvalidTransitionError,
)
from sabor_da_maria_agent.infrastructure.persistence.sqlite import (
    DB_PATH_ENV,
    SQLitePlanningRepository,
    default_db_path,
)

S = CandidateStatus


def _ingredient(name: str, grams: int, paid: str, bought_grams: int) -> IngredientCost:
    return IngredientCost(
        name=name,
        stock_quantity=Decimal(grams),
        stock_unit="g",
        stock_base_quantity=Decimal(grams),
        purchase_quantity=Decimal(bought_grams),
        purchase_unit="g",
        purchase_base_quantity=Decimal(bought_grams),
        purchase_total=Decimal(paid),
        base_unit="g",
        dimension=MeasureDimension.MASS,
        unit_cost=Decimal(paid) / Decimal(bought_grams),
    )


CATALOG = PantryCatalog(
    ingredients=(
        _ingredient("Arroz", 1000, "10.00", 1000),  # R$ 0.01/g
        _ingredient("Frango", 500, "30.00", 1000),  # R$ 0.03/g
    ),
    historical_purchase_total=Decimal("40.00"),
)

KITCHEN: dict[str, Any] = {
    "equipment": [{"name": "Panela de pressão", "available": True}],
    "equipment_inventory_complete": True,
    "skills": ["Refogar"],
    "skills_inventory_complete": True,
    "available_people": 1,
    "daily_available_minutes": 240,
    "target_servings_per_day": 4,
    "gas_available": True,
}


def recipe(recipe_id: str = "galinhada", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "recipe_id": recipe_id,
        "name": "Galinhada",
        "description": "Arroz com frango na panela de pressão.",
        "servings": 4,
        "ingredients": [
            {"name": "Arroz", "quantity": "400", "unit": "g"},
            {"name": "Frango", "quantity": "800", "unit": "g"},
        ],
        "required_equipment": [{"name": "Panela de pressão"}],
        "required_skills": ["refogar"],
        "required_utilities": ["gas"],
        "storage_requirements": [],
        "minimum_people": 1,
        "total_time_minutes": 60,
    }
    data.update(overrides)
    return data


SOURCES = [
    {
        "title": "Galinhada de panela de pressão",
        "url": "https://receitas.example/galinhada",
        "excerpt": "Arroz, frango, açafrão.",
        "retrieved_at": "2026-11-23T12:00:00-03:00",
    }
]


def quote(price: str = "25.00", name: str = "Frango") -> dict[str, str]:
    return {
        "name": name,
        "package_quantity": "1",
        "package_unit": "kg",
        "package_price": price,
        "source": f"https://mercado.example/{name.lower()}",
    }


class CatalogHolder:
    def __init__(self, catalog: PantryCatalog) -> None:
        self.catalog = catalog

    def __call__(self) -> PantryCatalog:
        return self.catalog


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "planning.sqlite3"


@pytest.fixture
def catalog_holder() -> CatalogHolder:
    return CatalogHolder(CATALOG)


@pytest.fixture
def make_service(
    db_path: Path, catalog_holder: CatalogHolder
) -> Callable[[], PlanningService]:
    """Each call simulates a fresh process: new repository, same database."""
    return lambda: PlanningService(SQLitePlanningRepository(db_path), catalog_holder)


@pytest.fixture
def service(make_service: Callable[[], PlanningService]) -> PlanningService:
    return make_service()


def register(
    service: PlanningService,
    session_id: str,
    data: dict[str, Any] | None = None,
    quotes: list[dict[str, str]] | None = None,
    **kwargs: Any,
) -> RecipeCandidate:
    return service.register_candidate(
        session_id,
        data or recipe(),
        SOURCES,
        [quote()] if quotes is None else quotes,
        **kwargs,
    )


def awaiting(service: PlanningService, **kwargs: Any) -> RecipeCandidate:
    session = service.start_session(KITCHEN)
    candidate = register(service, session.session_id, **kwargs)
    evaluated = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert evaluated.status == S.AWAITING_APPROVAL, evaluated.evaluation
    return evaluated


def approve(service: PlanningService, candidate: RecipeCandidate) -> RecipeCandidate:
    return service.decide_candidate(
        candidate.session_id,
        candidate.candidate_id,
        candidate.version,
        "approve",
        "Sim, quero fazer a galinhada.",
    )


def codes(candidate: RecipeCandidate, kind: str) -> set[str]:
    assert candidate.evaluation is not None
    return {issue.code for issue in getattr(candidate.evaluation, kind)}


# --------------------------------------------------------------- happy path


def test_full_workflow_reuses_existing_financial_calculators(
    service: PlanningService,
) -> None:
    candidate = awaiting(service)
    evaluation = candidate.evaluation
    assert evaluation is not None and evaluation.purchase_plan is not None
    plan = evaluation.purchase_plan
    assert plan.supplemental_purchase_total == Decimal("25.00")
    assert plan.budget_limit == Decimal("80.00")
    assert plan.budget_remaining_after_purchase == Decimal("55.00")
    frango = next(line for line in evaluation.pantry if line.name == "Frango")
    assert frango.shortfall_quantity == Decimal(300)
    assert frango.purchase_quote_provided

    approved = approve(service, candidate)
    assert approved.status == S.APPROVED
    assert approved.approval is not None
    assert approved.approval.candidate_version == candidate.version

    priced = service.calculate_approved(
        approved.session_id, approved.candidate_id, approved.version
    )
    assert priced.status == S.PRICED
    calc = priced.calculation
    assert calc is not None

    expected_cost = RecipeCostCalculator().calculate(
        recipe_name=priced.recipe.name,
        requirements=requirements_for(priced.recipe),
        catalog=CATALOG,
        supplemental_quotes=[SupplementalPurchaseQuote.model_validate(quote())],
        servings=4,
    )
    expected_report = DeliveryPricingCalculator().calculate(expected_cost)
    assert Decimal(calc["total_cmv_brl"]) == expected_cost.total_cmv == Decimal("26.5")
    assert Decimal(calc["cmv_per_portion_brl"]) == Decimal("6.625")
    assert calc["cmv_complete"] is True
    assert calc["pending_cost_items"] == []
    assert calc["delivery_pricing"] == expected_report.model_dump(mode="json")
    assert len(calc["delivery_pricing"]["per_portion"]["scenarios"]) == 3
    assert "packaging" in calc["excluded_costs"]

    _, _, events = service.get_status(priced.session_id, priced.candidate_id)
    hops = [(e.from_status, e.to_status) for e in events if e.to_status]
    assert hops == [
        (None, S.DISCOVERED),
        (S.DISCOVERED, S.EVALUATED),
        (S.EVALUATED, S.FEASIBLE),
        (S.FEASIBLE, S.AWAITING_APPROVAL),
        (S.AWAITING_APPROVAL, S.APPROVED),
        (S.APPROVED, S.COSTED),
        (S.COSTED, S.PRICED),
    ]


def test_source_evidence_is_persisted_as_unverified(service: PlanningService) -> None:
    session = service.start_session()
    candidate = register(service, session.session_id)
    (evidence,) = candidate.evidence
    assert str(evidence.url) == "https://receitas.example/galinhada"
    assert evidence.excerpt == "Arroz, frango, açafrão."
    assert evidence.retrieved_at is not None
    assert evidence.verification_status == "unverified_search_result"


def test_manual_source_review_is_distinguished_from_unverified_search(
    service: PlanningService,
) -> None:
    session = service.start_session()
    reviewed_source = {
        **SOURCES[0],
        "manual_review": {
            "reviewed_by": "Filipi",
            "reviewed_at": "2026-11-23T15:00:00-03:00",
            "note": "Pagina aberta e ingredientes conferidos visualmente.",
        },
    }
    candidate = service.register_candidate(
        session.session_id, recipe(), [reviewed_source], []
    )

    (evidence,) = candidate.evidence
    assert evidence.verification_status == "manually_reviewed"
    assert evidence.reviewed_by == "Filipi"
    assert evidence.reviewed_at is not None
    assert evidence.automated_verification is False


# ------------------------------------------------------- feasibility gates


def test_incomplete_kitchen_profile_needs_information(service: PlanningService) -> None:
    session = service.start_session()  # nothing elicited yet
    candidate = register(service, session.session_id)
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert result.status == S.NEEDS_INFORMATION
    assert {
        "target_servings_unknown",
        "available_time_unknown",
        "equipment_inventory_unknown",
        "skill_inventory_unknown",
        "utility_availability_unknown",
    } <= codes(result, "missing_information")
    assert result.approval is None


def test_unknown_recipe_requirements_are_not_feasible(service: PlanningService) -> None:
    session = service.start_session(KITCHEN)
    data = recipe()
    del data["required_equipment"], data["required_skills"]
    candidate = register(service, session.session_id, data)
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert result.status == S.NEEDS_INFORMATION
    assert {
        "recipe_equipment_requirements_unknown",
        "recipe_skill_requirements_unknown",
    } <= codes(result, "missing_information")


@pytest.mark.parametrize(
    ("kitchen_change", "blocker"),
    [
        (
            {"equipment": [{"name": "Panela de pressão", "available": False}]},
            "equipment_unavailable",
        ),
        ({"equipment": []}, "equipment_missing"),
        ({"skills": ["Assar"]}, "required_skill_missing"),
        ({"gas_available": False}, "utility_unavailable"),
        ({"daily_available_minutes": 30}, "production_window_exceeded"),
    ],
)
def test_unavailable_equipment_skills_or_constraints_are_infeasible(
    service: PlanningService, kitchen_change: dict[str, Any], blocker: str
) -> None:
    session = service.start_session({**KITCHEN, **kitchen_change})
    candidate = register(service, session.session_id)
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert result.status == S.INFEASIBLE
    assert blocker in codes(result, "blockers")
    with pytest.raises(InvalidTransitionError):
        approve(service, result)


# ------------------------------------------------ ingredients and purchases


def test_missing_ingredient_quantity_blocks_evaluation(
    service: PlanningService,
) -> None:
    session = service.start_session(KITCHEN)
    data = recipe()
    data["ingredients"] = [*data["ingredients"], {"name": "Sal"}]
    candidate = register(service, session.session_id, data)
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert result.status == S.NEEDS_INFORMATION
    assert "ingredient_quantity_unknown" in codes(result, "missing_information")
    assert result.evaluation is not None
    salt = next(line for line in result.evaluation.pantry if line.name == "Sal")
    assert salt.status == "quantity_unknown"
    assert salt.original_quantity is None and salt.original_unit is None
    with pytest.raises(InvalidTransitionError):
        approve(service, result)


def test_every_ingredient_is_reported_when_measurement_units_are_unsupported(
    service: PlanningService,
) -> None:
    session = service.start_session(KITCHEN)
    data = recipe()
    data["ingredients"] = [
        {"name": "Arroz", "quantity": "400", "unit": "g"},
        {"name": "Frango", "quantity": "3", "unit": "dente"},
        {"name": "Alho", "quantity": "2", "unit": "dente"},
        {"name": "Óleo", "quantity": "1", "unit": "colher"},
        {"name": "Orégano", "quantity": "1", "unit": "mão cheia"},
    ]
    candidate = register(service, session.session_id, data, quotes=[])
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )

    assert result.status == S.NEEDS_INFORMATION
    assert result.evaluation is not None
    lines = result.evaluation.pantry
    assert [line.name for line in lines] == [
        "Arroz",
        "Frango",
        "Alho",
        "Óleo",
        "Orégano",
    ]
    unsupported = {line.name for line in lines if line.status == "unit_unsupported"}
    assert unsupported == {"Frango", "Alho", "Óleo", "Orégano"}
    assert result.evaluation.purchase_plan is None
    assert "ingredient_unit_unsupported" in codes(result, "missing_information")


def test_incompatible_ingredient_dimension_is_explicit_and_not_zeroed(
    service: PlanningService,
) -> None:
    session = service.start_session(KITCHEN)
    data = recipe()
    data["ingredients"] = [
        {"name": "Arroz", "quantity": "400", "unit": "g"},
        {"name": "Frango", "quantity": "2", "unit": "un"},
    ]
    candidate = register(service, session.session_id, data, quotes=[])
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )

    assert result.status == S.NEEDS_INFORMATION
    assert result.evaluation is not None
    chicken = next(line for line in result.evaluation.pantry if line.name == "Frango")
    assert chicken.status == "measurement_incompatible"
    assert chicken.issue_code == "ingredient_measurement_incompatible"
    assert chicken.original_quantity == Decimal(2)
    assert chicken.original_unit == "un"
    assert chicken.required_quantity is None
    assert chicken.shortfall_quantity is None


def test_partial_cmv_does_not_hide_unsupported_units(service: PlanningService) -> None:
    session = service.start_session(KITCHEN)
    data = recipe()
    data["ingredients"] = [
        {"name": "Arroz", "quantity": "400", "unit": "g"},
        {"name": "Frango", "quantity": "2", "unit": "dente"},
    ]
    candidate = register(
        service, session.session_id, data, quotes=[], allow_partial_cmv=True
    )
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )

    assert result.status == S.NEEDS_INFORMATION
    assert result.evaluation is not None
    assert result.evaluation.pending_cost_items == ()
    assert any(
        line.name == "Frango" and line.status == "unit_unsupported"
        for line in result.evaluation.pantry
    )
    assert result.evaluation.purchase_plan is None


def test_explicitly_accepted_pending_item_yields_partial_cmv(
    service: PlanningService,
) -> None:
    data = recipe()
    data["ingredients"] = [*data["ingredients"], {"name": "Sal"}]
    candidate = approve(service, awaiting(service, data=data, allow_partial_cmv=True))
    priced = service.calculate_approved(
        candidate.session_id, candidate.candidate_id, candidate.version
    )
    assert priced.calculation is not None
    assert priced.calculation["cmv_complete"] is False
    assert priced.calculation["pending_cost_items"] == ["Sal"]


def test_missing_purchase_quote_needs_information(service: PlanningService) -> None:
    session = service.start_session(KITCHEN)
    candidate = register(service, session.session_id, quotes=[])
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert result.status == S.NEEDS_INFORMATION
    assert "purchase_quote_missing" in codes(result, "missing_information")
    assert result.evaluation is not None and result.evaluation.purchase_plan is None


def test_ingredient_absent_from_pantry_requires_quote_then_is_purchased(
    service: PlanningService,
) -> None:
    session = service.start_session(KITCHEN)
    data = recipe()
    data["ingredients"] = [
        *data["ingredients"],
        {"name": "Cebola", "quantity": "100", "unit": "g"},
    ]
    candidate = register(service, session.session_id, data)
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert result.status == S.NEEDS_INFORMATION
    assert result.evaluation is not None
    cebola = next(line for line in result.evaluation.pantry if line.name == "Cebola")
    assert not cebola.in_pantry
    assert cebola.shortfall_quantity == Decimal(100)

    revised = register(
        service,
        session.session_id,
        data,
        quotes=[quote(), quote("6.00", "Cebola")],
        candidate_id=result.candidate_id,
        expected_candidate_version=result.version,
    )
    evaluated = service.evaluate_candidate(
        session.session_id, revised.candidate_id, revised.version
    )
    assert evaluated.status == S.AWAITING_APPROVAL
    assert evaluated.evaluation is not None
    plan = evaluated.evaluation.purchase_plan
    assert plan is not None
    assert plan.supplemental_purchase_total == Decimal("31.00")


def test_purchase_exceeding_r80_budget_is_infeasible(service: PlanningService) -> None:
    session = service.start_session(KITCHEN)
    candidate = register(service, session.session_id, quotes=[quote("90.00")])
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert result.status == S.INFEASIBLE
    assert "purchase_budget_exceeded" in codes(result, "blockers")


def test_r80_budget_is_shared_between_approved_candidates(
    service: PlanningService,
) -> None:
    session = service.start_session(KITCHEN)
    first = register(service, session.session_id, recipe("a"), [quote("50.00")])
    second = register(service, session.session_id, recipe("b"), [quote("50.00")])
    first = service.evaluate_candidate(
        session.session_id, first.candidate_id, first.version
    )
    second = service.evaluate_candidate(
        session.session_id, second.candidate_id, second.version
    )
    assert first.status == second.status == S.AWAITING_APPROVAL

    approve(service, first)
    with pytest.raises(BudgetExceededError, match="shared budget"):
        approve(service, second)
    _, (second_after,), _ = service.get_status(second.session_id, second.candidate_id)
    assert second_after.status == S.DISCOVERED
    assert second_after.evaluation is None and second_after.approval is None

    third = register(service, session.session_id, recipe("c"), [quote("50.00")])
    third = service.evaluate_candidate(
        session.session_id, third.candidate_id, third.version
    )
    assert third.status == S.INFEASIBLE
    assert third.evaluation is not None and third.evaluation.purchase_plan is not None
    assert third.evaluation.purchase_plan.committed_by_other_candidates == Decimal(
        "50.00"
    )


# ------------------------------------------------------- approval gating


def test_approval_and_costing_require_prior_steps(service: PlanningService) -> None:
    session = service.start_session(KITCHEN)
    candidate = register(service, session.session_id)
    with pytest.raises(InvalidTransitionError):
        approve(service, candidate)
    with pytest.raises(ApprovalError, match="explicit approval"):
        service.calculate_approved(
            session.session_id, candidate.candidate_id, candidate.version
        )

    waiting = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    with pytest.raises(ApprovalError, match="awaiting_approval"):
        service.calculate_approved(
            session.session_id, waiting.candidate_id, waiting.version
        )


def test_rejected_candidate_cannot_be_costed(service: PlanningService) -> None:
    candidate = awaiting(service)
    rejected = service.decide_candidate(
        candidate.session_id,
        candidate.candidate_id,
        candidate.version,
        "reject",
        "Não.",
    )
    assert rejected.status == S.REJECTED
    with pytest.raises(ApprovalError):
        service.calculate_approved(
            rejected.session_id, rejected.candidate_id, rejected.version
        )


def test_recipe_revision_invalidates_approval(service: PlanningService) -> None:
    candidate = awaiting(service)
    approved = approve(service, candidate)
    revised = register(
        service,
        approved.session_id,
        recipe(servings=5),
        candidate_id=approved.candidate_id,
        expected_candidate_version=approved.version,
    )
    assert revised.status == S.DISCOVERED
    assert revised.approval is None and revised.evaluation is None

    with pytest.raises(StaleVersionError):
        service.calculate_approved(
            revised.session_id, revised.candidate_id, approved.version
        )
    with pytest.raises(StaleVersionError):
        approve(service, candidate)  # approval of the pre-revision version
    with pytest.raises(ApprovalError):
        service.calculate_approved(
            revised.session_id, revised.candidate_id, revised.version
        )


def test_kitchen_change_invalidates_approval(service: PlanningService) -> None:
    approved = approve(service, awaiting(service))
    session, _, _ = service.get_status(approved.session_id)
    updated, invalidated = service.update_kitchen_profile(
        session.session_id, session.version, {"daily_available_minutes": 300}
    )
    assert invalidated == [approved.candidate_id]
    assert updated.kitchen_profile is not None
    assert updated.kitchen_profile.daily_available_minutes == 300
    assert updated.kitchen_profile.skills == ("Refogar",)  # merge kept other facts
    _, (current,), _ = service.get_status(session.session_id, approved.candidate_id)
    assert current.status == S.DISCOVERED and current.approval is None


def test_pantry_change_after_approval_resets_candidate(
    service: PlanningService, catalog_holder: CatalogHolder
) -> None:
    approved = approve(service, awaiting(service))
    catalog_holder.catalog = PantryCatalog(
        ingredients=(
            _ingredient("Arroz", 1000, "12.00", 1000),
            _ingredient("Frango", 500, "30.00", 1000),
        ),
        historical_purchase_total=Decimal("42.00"),
    )
    with pytest.raises(ApprovalError, match="changed since approval"):
        service.calculate_approved(
            approved.session_id, approved.candidate_id, approved.version
        )
    _, (current,), _ = service.get_status(approved.session_id, approved.candidate_id)
    assert current.status == S.DISCOVERED  # invalidation was committed


# --------------------------------------------- idempotency and persistence


def test_repeated_approval_and_calculation_are_idempotent(
    service: PlanningService,
) -> None:
    candidate = awaiting(service)
    first = approve(service, candidate)
    events_before = len(service.get_status(first.session_id)[2])
    second = approve(service, candidate)
    assert second.version == first.version
    assert len(service.get_status(first.session_id)[2]) == events_before

    priced = service.calculate_approved(
        first.session_id, first.candidate_id, first.version
    )
    again = service.calculate_approved(
        first.session_id, first.candidate_id, first.version
    )
    assert again.version == priced.version
    assert again.calculation == priced.calculation

    with pytest.raises(StaleVersionError):  # same version, opposite decision
        service.decide_candidate(
            candidate.session_id,
            candidate.candidate_id,
            candidate.version,
            "reject",
            "x",
        )


def test_state_survives_repository_and_service_restart(
    make_service: Callable[[], PlanningService],
) -> None:
    candidate = awaiting(make_service())
    approve(make_service(), candidate)

    restarted = make_service()
    session, (loaded,), events = restarted.get_status(candidate.session_id)
    assert session.kitchen_profile is not None
    assert loaded.status == S.APPROVED
    assert loaded.approval is not None and loaded.evaluation is not None
    assert loaded.recipe == candidate.recipe
    assert loaded.evidence == candidate.evidence
    assert any(e.event_type == "candidate_approved" for e in events)

    priced = make_service().calculate_approved(
        loaded.session_id, loaded.candidate_id, loaded.version
    )
    assert priced.status == S.PRICED


# ------------------------------------------------ production plan regressions


def test_twelve_yield_for_fifteen_target_uses_two_batches_everywhere(
    service: PlanningService,
) -> None:
    session = service.start_session({**KITCHEN, "target_servings_per_day": 15})
    data = recipe(servings=12)
    confirmation = {
        "confirmed_batch_count": 2,
        "production_minutes": 150,
        "user_confirmation": "Confirmo dois lotes, total de 150 minutos.",
    }
    candidate = register(
        service,
        session.session_id,
        data,
        production_confirmation=confirmation,
    )
    evaluated = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )

    assert evaluated.status == S.AWAITING_APPROVAL
    assert evaluated.evaluation is not None
    plan = evaluated.evaluation.production_plan
    assert plan is not None
    assert plan.source_recipe_yield == 12
    assert plan.target_daily_servings == 15
    assert plan.planned_batch_size == 12
    assert plan.batch_count == 2
    assert plan.total_produced_servings == 24
    assert plan.surplus_servings == 9

    rice = next(line for line in evaluated.evaluation.pantry if line.name == "Arroz")
    chicken = next(
        line for line in evaluated.evaluation.pantry if line.name == "Frango"
    )
    assert rice.required_quantity == Decimal(800)
    assert chicken.required_quantity == Decimal(1600)
    assert chicken.stock_available_quantity == Decimal(500)
    assert chicken.shortfall_quantity == Decimal(1100)
    assert evaluated.evaluation.purchase_plan is not None
    assert evaluated.evaluation.purchase_plan.supplemental_purchase_total == Decimal(
        "50.00"
    )

    approved = approve(service, evaluated)
    priced = service.calculate_approved(
        approved.session_id, approved.candidate_id, approved.version
    )
    assert priced.calculation is not None
    assert priced.calculation["production_plan"] == plan.model_dump(mode="json")
    assert priced.calculation["servings"] == 24


def test_production_confirmation_survives_repository_restart(
    make_service: Callable[[], PlanningService],
) -> None:
    service = make_service()
    session = service.start_session({**KITCHEN, "target_servings_per_day": 8})
    confirmation = {
        "confirmed_batch_count": 2,
        "production_minutes": 120,
        "user_confirmation": "Confirmo dois lotes em 120 minutos.",
    }
    candidate = register(service, session.session_id)
    revised = register(
        service,
        session.session_id,
        candidate_id=candidate.candidate_id,
        expected_candidate_version=candidate.version,
        production_confirmation=confirmation,
    )

    restarted = make_service()
    _, (loaded,), _ = restarted.get_status(session.session_id, revised.candidate_id)

    assert loaded.production_confirmation is not None
    assert loaded.production_confirmation.confirmed_batch_count == 2
    assert loaded.production_confirmation.production_minutes == 120
    assert loaded.production_confirmation.user_confirmation == (
        "Confirmo dois lotes em 120 minutos."
    )


def test_v1_database_migrates_without_losing_existing_candidates(
    service: PlanningService, db_path: Path, catalog_holder: CatalogHolder
) -> None:
    session = service.start_session(KITCHEN)
    candidate = register(service, session.session_id)
    evaluated = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert evaluated.status == S.AWAITING_APPROVAL

    # Simulate a database created by schema v1, before the confirmation field.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "ALTER TABLE recipe_candidates DROP COLUMN production_confirmation_json"
        )
        connection.execute("PRAGMA user_version = 1")

    migrated = SQLitePlanningRepository(db_path)
    restarted = PlanningService(migrated, catalog_holder)
    _, (loaded,), events = restarted.get_status(
        session.session_id, candidate.candidate_id
    )

    assert loaded.candidate_id == candidate.candidate_id
    assert loaded.status == S.DISCOVERED
    assert loaded.version == evaluated.version + 1
    assert loaded.evaluation is None and loaded.approval is None
    assert loaded.production_confirmation is None
    assert any(
        event.event_type == "candidate_invalidated_by_schema_migration"
        for event in events
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(recipe_candidates)")
        }
    assert "production_confirmation_json" in columns


# ------------------------------------------------------------ concurrency


def _race(workers: list[Callable[[], object]]) -> list[object]:
    barrier = threading.Barrier(len(workers))
    results: list[object] = [None] * len(workers)

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = workers[index]()
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_concurrent_conflicting_decisions_only_one_wins(
    make_service: Callable[[], PlanningService],
) -> None:
    candidate = awaiting(make_service())

    def decide(decision: str) -> Callable[[], object]:
        return lambda: make_service().decide_candidate(
            candidate.session_id,
            candidate.candidate_id,
            candidate.version,
            decision,
            "decisão",
        )

    results = _race([decide("approve"), decide("reject")])
    winners = [r for r in results if isinstance(r, RecipeCandidate)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1 and len(losers) == 1
    assert isinstance(losers[0], StaleVersionError)
    _, (final,), _ = make_service().get_status(candidate.session_id)
    assert final.status == winners[0].status
    assert final.version == candidate.version + 1


def test_concurrent_kitchen_updates_use_optimistic_locking(
    make_service: Callable[[], PlanningService],
) -> None:
    session = make_service().start_session(KITCHEN)
    workers: list[Callable[[], object]] = [
        (
            lambda minutes=minutes: make_service().update_kitchen_profile(
                session.session_id,
                session.version,
                {"daily_available_minutes": minutes},
            )
        )
        for minutes in (100, 200, 300, 400)
    ]
    results = _race(workers)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(
        isinstance(r, StaleVersionError) for r in results if isinstance(r, Exception)
    )
    assert make_service().get_status(session.session_id)[0].version == 2


def test_repository_rejects_write_with_stale_version(db_path: Path) -> None:
    service = PlanningService(SQLitePlanningRepository(db_path), lambda: CATALOG)
    session = service.start_session()
    candidate = register(service, session.session_id)
    repository = SQLitePlanningRepository(db_path)
    with pytest.raises(StaleVersionError), repository.transaction() as uow:
        uow.update_candidate(candidate, expected_version=candidate.version + 7)


# ------------------------------------------------------ adversarial input


@pytest.mark.parametrize(
    "bad_id",
    ["", "x' OR '1'='1", "A" * 32, "0" * 31, "0" * 33, "../../etc/passwd", None, 7],
)
def test_malformed_ids_are_rejected(service: PlanningService, bad_id: Any) -> None:
    with pytest.raises(PlanningError, match="hex id"):
        service.get_status(bad_id)


@pytest.mark.parametrize("bad_version", [0, -1, True, "1", 1.0])
def test_malformed_versions_are_rejected(
    service: PlanningService, bad_version: Any
) -> None:
    session = service.start_session()
    candidate = register(service, session.session_id)
    with pytest.raises(PlanningError, match="positive integer"):
        service.evaluate_candidate(
            session.session_id, candidate.candidate_id, bad_version
        )


def test_candidate_from_another_session_is_not_found(service: PlanningService) -> None:
    owner = service.start_session(KITCHEN)
    other = service.start_session(KITCHEN)
    candidate = register(service, owner.session_id)
    with pytest.raises(NotFoundError):
        service.evaluate_candidate(other.session_id, candidate.candidate_id, 1)


@pytest.mark.parametrize(
    ("recipe_data", "sources", "quotes", "error"),
    [
        (recipe(unexpected="x"), SOURCES, [], ValidationError),
        (recipe(sources=SOURCES), SOURCES, [], PlanningError),
        (recipe(servings=0), SOURCES, [], ValidationError),
        (recipe(), [], [], PlanningError),
        (recipe(), [{**SOURCES[0], "url": "javascript:alert(1)"}], [], ValidationError),
        (recipe(), SOURCES * 2, [], PlanningError),
        (recipe(), SOURCES, [{**quote(), "package_price": 25.0}], PlanningError),
        (recipe(), SOURCES, [{**quote(), "package_price": "-1"}], ValidationError),
        (
            recipe(),
            SOURCES,
            [{**quote(), "source": "mercado do bairro"}],
            ValidationError,
        ),
        (recipe(), SOURCES, [quote(name="Picanha")], PlanningError),
        (recipe(), SOURCES, [quote(), quote()], PlanningError),
        (
            recipe(ingredients=[{"name": "Arroz", "quantity": "1", "unit": "g"}] * 2),
            SOURCES,
            [],
            PlanningError,
        ),
    ],
)
def test_adversarial_candidate_payloads_are_rejected(
    service: PlanningService,
    recipe_data: dict[str, Any],
    sources: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    error: type[Exception],
) -> None:
    session = service.start_session()
    with pytest.raises(error):
        service.register_candidate(session.session_id, recipe_data, sources, quotes)
    assert service.get_status(session.session_id)[1] == []


def test_duplicate_recipe_id_requires_revision(service: PlanningService) -> None:
    session = service.start_session()
    register(service, session.session_id)
    with pytest.raises(PlanningError, match="already registered"):
        register(service, session.session_id)


def test_sql_is_parameterized(db_path: Path) -> None:
    repository = SQLitePlanningRepository(db_path)
    with repository.transaction() as uow:
        assert uow.get_session("x'; DROP TABLE planning_sessions; --") is None
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"planning_sessions", "recipe_candidates", "workflow_events"} <= tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_foreign_keys_are_enforced(db_path: Path) -> None:
    repository = SQLitePlanningRepository(db_path)
    service = PlanningService(repository, lambda: CATALOG)
    session = service.start_session()
    candidate = register(service, session.session_id)
    orphan = candidate.__class__(
        **{
            **{f: getattr(candidate, f) for f in candidate.__dataclass_fields__},
            "candidate_id": "f" * 32,
            "session_id": "e" * 32,
        }
    )
    with pytest.raises(sqlite3.IntegrityError), repository.transaction() as uow:
        uow.insert_candidate(orphan)


def test_db_path_environment_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "custom" / "db.sqlite3"))
    assert default_db_path() == (tmp_path / "custom" / "db.sqlite3").resolve()
    monkeypatch.delenv(DB_PATH_ENV)
    default = default_db_path()
    assert default.parts[-2:] == ("sabor-da-maria-agent", "planning.sqlite3")


# ------------------------------------- Staff audit: planning and stock gates


def test_target_change_with_same_batch_count_invalidates_production_confirmation(
    service: PlanningService,
) -> None:
    session = service.start_session({**KITCHEN, "target_servings_per_day": 15})
    original_confirmation = {
        "confirmed_batch_count": 2,
        "production_minutes": 150,
        "user_confirmation": "Confirmo 16 porcoes, 1 extra, em 150 minutos.",
    }
    candidate = register(
        service,
        session.session_id,
        recipe(servings=8),
        production_confirmation=original_confirmation,
    )
    assert candidate.production_confirmation is not None
    assert candidate.production_confirmation.confirmed_target_daily_servings == 15
    assert candidate.production_confirmation.confirmed_source_recipe_yield == 8
    evaluated = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, candidate.version
    )
    assert evaluated.status == S.AWAITING_APPROVAL

    # 9 and 15 portions both require exactly TWO 8-portion batches, but
    # the surplus is 7 rather than 1. Old confirmation must not apply.
    session = service.update_kitchen_profile(
        session.session_id, session.version, {"target_servings_per_day": 9}
    )[0]
    _, (invalidated,), _ = service.get_status(
        session.session_id, candidate.candidate_id
    )
    assert invalidated.status == S.DISCOVERED
    assert invalidated.production_confirmation is not None
    assert invalidated.production_confirmation.confirmed_target_daily_servings == 15
    result = service.evaluate_candidate(
        session.session_id, candidate.candidate_id, invalidated.version
    )
    assert result.status == S.NEEDS_INFORMATION
    assert {
        "production_confirmation_mismatch",
        "production_surplus_unconfirmed",
        "multi_batch_duration_unconfirmed",
    } <= codes(result, "missing_information")
    with pytest.raises(InvalidTransitionError):
        approve(service, result)

    # A new explicit user confirmation binds to the new target.
    revised = register(
        service,
        session.session_id,
        recipe(servings=8),
        candidate_id=result.candidate_id,
        expected_candidate_version=result.version,
        production_confirmation={
            "confirmed_batch_count": 2,
            "production_minutes": 165,
            "user_confirmation": "Confirmo 16 porcoes, 7 extras, em 165 minutos.",
        },
    )
    assert (
        service.evaluate_candidate(
            revised.session_id, revised.candidate_id, revised.version
        ).status
        == S.AWAITING_APPROVAL
    )


def test_approval_rejects_changed_pantry_and_commits_invalidation(
    service: PlanningService,
    catalog_holder: CatalogHolder,
) -> None:
    candidate = awaiting(service)
    catalog_holder.catalog = PantryCatalog(
        ingredients=(
            _ingredient("Arroz", 1000, "10.00", 1000),
            _ingredient("Frango", 100, "30.00", 1000),
        ),
        historical_purchase_total=Decimal("40.00"),
    )
    with pytest.raises(ApprovalError, match="changed since evaluation"):
        approve(service, candidate)
    _, (saved,), events = service.get_status(
        candidate.session_id, candidate.candidate_id
    )
    assert saved.status == S.DISCOVERED
    assert saved.evaluation is None and saved.approval is None
    assert events[-1].event_type == "approval_invalidated"


def test_approval_rejects_new_stock_reservation_even_when_budget_allows(
    service: PlanningService,
) -> None:
    session = service.start_session(KITCHEN)
    first = register(
        service,
        session.session_id,
        recipe("r1", ingredients=[{"name": "Arroz", "quantity": "800", "unit": "g"}]),
        quotes=[],
    )
    second = register(
        service,
        session.session_id,
        recipe("r2", ingredients=[{"name": "Arroz", "quantity": "100", "unit": "g"}]),
        quotes=[],
    )
    first = service.evaluate_candidate(
        session.session_id, first.candidate_id, first.version
    )
    second = service.evaluate_candidate(
        session.session_id, second.candidate_id, second.version
    )
    assert first.status == second.status == S.AWAITING_APPROVAL
    approve(service, first)
    with pytest.raises(ApprovalError, match="other approved recipes changed"):
        approve(service, second)
    _, (saved,), _ = service.get_status(second.session_id, second.candidate_id)
    assert saved.status == S.DISCOVERED
    reviewed = service.evaluate_candidate(
        saved.session_id, saved.candidate_id, saved.version
    )
    assert reviewed.status == S.AWAITING_APPROVAL
    assert reviewed.evaluation is not None
    rice = reviewed.evaluation.pantry[0]
    assert rice.stock_available_quantity == Decimal(200)
    assert rice.shortfall_quantity == 0
    approve(service, reviewed)


def test_approved_recipes_reserve_stock_and_keep_prior_cost_allocation(
    service: PlanningService,
) -> None:
    session = service.start_session(KITCHEN)
    first = register(
        service,
        session.session_id,
        recipe(
            "rice-1", ingredients=[{"name": "Arroz", "quantity": "800", "unit": "g"}]
        ),
        quotes=[],
    )
    first = service.evaluate_candidate(
        session.session_id, first.candidate_id, first.version
    )
    first = approve(service, first)

    second = register(
        service,
        session.session_id,
        recipe(
            "rice-2", ingredients=[{"name": "Arroz", "quantity": "700", "unit": "g"}]
        ),
        quotes=[quote("30.00", "Arroz")],
    )
    second = service.evaluate_candidate(
        session.session_id, second.candidate_id, second.version
    )
    assert second.status == S.AWAITING_APPROVAL
    assert second.evaluation is not None
    rice = second.evaluation.pantry[0]
    assert rice.stock_available_quantity == Decimal(200)
    assert rice.shortfall_quantity == Decimal(500)
    assert second.evaluation.purchase_plan is not None
    assert second.evaluation.purchase_plan.supplemental_purchase_total == Decimal(30)
    second = approve(service, second)

    # The SECOND approval must not steal the first recipe's reserved rice.
    first_priced = service.calculate_approved(
        first.session_id, first.candidate_id, first.version
    )
    assert first_priced.calculation is not None
    assert Decimal(first_priced.calculation["total_cmv_brl"]) == Decimal("8.00")
    second_priced = service.calculate_approved(
        second.session_id, second.candidate_id, second.version
    )
    assert second_priced.calculation is not None
    assert Decimal(second_priced.calculation["total_cmv_brl"]) == Decimal("17.00")
    assert Decimal(
        second_priced.calculation["supplemental_purchase_total_brl"]
    ) == Decimal(30)

    # The source 1 kg has now been consumed; a third recipe cannot claim it.
    third = register(
        service,
        session.session_id,
        recipe(
            "rice-3", ingredients=[{"name": "Arroz", "quantity": "100", "unit": "g"}]
        ),
        quotes=[quote("30.00", "Arroz")],
    )
    third = service.evaluate_candidate(
        session.session_id, third.candidate_id, third.version
    )
    assert third.evaluation is not None
    assert third.evaluation.pantry[0].stock_available_quantity == Decimal(0)
    assert third.evaluation.pantry[0].shortfall_quantity == Decimal(100)
    assert third.evaluation.purchase_plan is not None
    assert third.evaluation.purchase_plan.committed_by_other_candidates == Decimal(30)


def test_approval_event_order_not_equal_clock_timestamp_controls_stock(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    frozen = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    counter = iter(["f" * 32, "e" * 32, "d" * 32])
    service = PlanningService(
        SQLitePlanningRepository(tmp_path / "clock.db"),
        lambda: CATALOG,
        clock=lambda: frozen,
        id_factory=lambda: next(counter),
    )
    session = service.start_session(KITCHEN)
    first = register(
        service,
        session.session_id,
        recipe(
            "first", ingredients=[{"name": "Arroz", "quantity": "800", "unit": "g"}]
        ),
        quotes=[],
    )
    first = service.evaluate_candidate(
        session.session_id, first.candidate_id, first.version
    )
    first = approve(service, first)
    second = register(
        service,
        session.session_id,
        recipe(
            "second", ingredients=[{"name": "Arroz", "quantity": "100", "unit": "g"}]
        ),
        quotes=[],
    )
    second = service.evaluate_candidate(
        session.session_id, second.candidate_id, second.version
    )
    second = approve(service, second)
    # Lexicographic candidate IDs would assign the 'e' candidate priority
    # over 'f', but SQLite approval event IDs preserve true approval order.
    first = service.calculate_approved(
        first.session_id, first.candidate_id, first.version
    )
    second = service.calculate_approved(
        second.session_id, second.candidate_id, second.version
    )
    assert first.calculation is not None and second.calculation is not None
    assert Decimal(first.calculation["total_cmv_brl"]) == Decimal("8.00")
    assert Decimal(second.calculation["total_cmv_brl"]) == Decimal("1.00")


def test_v2_approvals_are_migrated_and_invalidated_for_stock_reservations(
    db_path: Path,
    catalog_holder: CatalogHolder,
) -> None:
    service = PlanningService(SQLitePlanningRepository(db_path), catalog_holder)
    approved = approve(service, awaiting(service))
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 2")
    reloaded = PlanningService(SQLitePlanningRepository(db_path), catalog_holder)
    _, (candidate,), events = reloaded.get_status(
        approved.session_id, approved.candidate_id
    )
    assert candidate.status == S.DISCOVERED
    assert candidate.approval is None and candidate.evaluation is None
    assert any(
        e.event_type == "candidate_invalidated_by_schema_migration" for e in events
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
