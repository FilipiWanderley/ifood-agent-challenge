from decimal import Decimal

import pytest
from pydantic import ValidationError

from sabor_da_maria_agent.domain.feasibility import (
    FeasibilityAssessment,
    FeasibilityEvaluator,
    FeasibilityStatus,
)
from sabor_da_maria_agent.domain.kitchen import (
    EquipmentRequirement,
    KitchenEquipment,
    KitchenProfile,
    UtilityKind,
)
from sabor_da_maria_agent.domain.recipe import Recipe, RecipeIngredient, RecipeSource


def make_recipe(**overrides: object) -> Recipe:
    data: dict[str, object] = {
        "recipe_id": "galinhada",
        "name": "Galinhada",
        "description": "Frango e arroz preparados na mesma panela.",
        "servings": 8,
        "ingredients": (
            RecipeIngredient(name="Frango", quantity=Decimal(1000), unit="g"),
            RecipeIngredient(name="Sal"),
        ),
        "sources": (
            RecipeSource.model_validate(
                {"title": "Galinhada", "url": "https://example.com/galinhada"}
            ),
        ),
        "required_equipment": (
            EquipmentRequirement(
                name="Panela de pressão",
                minimum_capacity=Decimal(5),
                capacity_unit="l",
            ),
            EquipmentRequirement(
                name="Fogão",
                minimum_capacity=Decimal(4),
                capacity_unit="bocas",
            ),
        ),
        "required_skills": ("galinhada",),
        "required_utilities": (UtilityKind.GAS, UtilityKind.WATER),
        "storage_requirements": (),
        "minimum_people": 1,
        "total_time_minutes": 90,
    }
    data.update(overrides)
    return Recipe.model_validate(data)


def make_kitchen(**overrides: object) -> KitchenProfile:
    data: dict[str, object] = {
        "equipment": (
            KitchenEquipment(
                name="Panela de pressão",
                available=True,
                capacity=Decimal(6),
                capacity_unit="l",
            ),
            KitchenEquipment(
                name="Fogão",
                available=True,
                capacity=Decimal(4),
                capacity_unit="bocas",
            ),
        ),
        "equipment_inventory_complete": True,
        "skills": ("GALINHADA",),
        "skills_inventory_complete": True,
        "daily_available_minutes": 360,
        "target_servings_per_day": 8,
        "available_people": 1,
        "gas_available": True,
        "water_available": True,
    }
    data.update(overrides)
    if data["skills"] is None and "skills_inventory_complete" not in overrides:
        data["skills_inventory_complete"] = None
    return KitchenProfile.model_validate(data)


def test_evaluates_feasible_single_batch_without_inventing_operational_facts() -> None:
    result = FeasibilityEvaluator().evaluate(make_recipe(), make_kitchen())

    assert result.status == FeasibilityStatus.FEASIBLE
    assert result.required_batches == 1
    assert result.production_plan is not None
    assert result.production_plan.total_produced_servings == 8
    assert result.estimated_total_minutes == 90
    assert result.blockers == ()
    assert result.missing_information == ()
    assert any("Sal" in warning for warning in result.warnings)


def test_unknown_kitchen_facts_return_needs_information_not_infeasible() -> None:
    result = FeasibilityEvaluator().evaluate(make_recipe(), KitchenProfile())

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert result.blockers == ()
    codes = {issue.code for issue in result.missing_information}
    assert "equipment_inventory_unknown" in codes
    assert "skill_inventory_unknown" in codes
    assert "target_servings_unknown" in codes
    assert "available_time_unknown" in codes
    assert "utility_availability_unknown" in codes


def test_known_missing_equipment_is_a_blocker() -> None:
    kitchen = make_kitchen(equipment=(), equipment_inventory_complete=True)

    result = FeasibilityEvaluator().evaluate(make_recipe(), kitchen)

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert {issue.code for issue in result.blockers} == {"equipment_missing"}


def test_unavailable_equipment_is_a_blocker() -> None:
    equipment = (
        KitchenEquipment(
            name="Panela de pressão",
            available=False,
            capacity=Decimal(6),
            capacity_unit="l",
        ),
        KitchenEquipment(
            name="Fogão",
            available=True,
            capacity=Decimal(4),
            capacity_unit="bocas",
        ),
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(equipment=equipment)
    )

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "equipment_unavailable" in {issue.code for issue in result.blockers}


def test_unknown_equipment_availability_requires_information() -> None:
    equipment = (
        KitchenEquipment(
            name="Panela de pressão",
            available=None,
            capacity=Decimal(6),
            capacity_unit="l",
        ),
        KitchenEquipment(
            name="Fogão",
            available=True,
            capacity=Decimal(4),
            capacity_unit="bocas",
        ),
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(equipment=equipment)
    )

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "equipment_availability_unknown" in {
        issue.code for issue in result.missing_information
    }


def test_insufficient_equipment_capacity_is_a_blocker() -> None:
    equipment = (
        KitchenEquipment(
            name="Panela de pressão",
            available=True,
            capacity=Decimal(3),
            capacity_unit="l",
        ),
        KitchenEquipment(
            name="Fogão",
            available=True,
            capacity=Decimal(4),
            capacity_unit="bocas",
        ),
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(equipment=equipment)
    )

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "equipment_capacity_insufficient" in {
        issue.code for issue in result.blockers
    }


def test_incompatible_equipment_capacity_unit_requires_information() -> None:
    equipment = (
        KitchenEquipment(
            name="Panela de pressão",
            available=True,
            capacity=Decimal(6),
            capacity_unit="qt",
        ),
        KitchenEquipment(
            name="Fogão",
            available=True,
            capacity=Decimal(4),
            capacity_unit="bocas",
        ),
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(equipment=equipment)
    )

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "equipment_capacity_unit_incompatible" in {
        issue.code for issue in result.missing_information
    }


def test_missing_known_skill_is_a_blocker() -> None:
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(skills=("arroz",))
    )

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "required_skill_missing" in {issue.code for issue in result.blockers}


def test_unavailable_required_utility_is_a_blocker() -> None:
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(gas_available=False)
    )

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "utility_unavailable" in {issue.code for issue in result.blockers}


def test_production_window_exceeded_is_a_blocker() -> None:
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(daily_available_minutes=60)
    )

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "production_window_exceeded" in {issue.code for issue in result.blockers}


def test_more_than_one_person_required_is_checked() -> None:
    recipe = make_recipe(minimum_people=2)
    result = FeasibilityEvaluator().evaluate(recipe, make_kitchen(available_people=1))

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "insufficient_people" in {issue.code for issue in result.blockers}


def test_blocker_takes_precedence_but_unknowns_remain_auditable() -> None:
    result = FeasibilityEvaluator().evaluate(
        make_recipe(),
        make_kitchen(
            equipment=(), equipment_inventory_complete=True, gas_available=None
        ),
    )

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert result.blockers
    assert result.missing_information


def test_assessment_rejects_inconsistent_status() -> None:
    with pytest.raises(ValidationError):
        FeasibilityAssessment(
            recipe_id="galinhada",
            status=FeasibilityStatus.NEEDS_INFORMATION,
            blockers=(),
            missing_information=(),
            warnings=(),
        )


def test_unknown_equipment_availability_still_reports_unknown_capacity() -> None:
    equipment = (
        KitchenEquipment(name="Panela de pressão", available=None),
        KitchenEquipment(
            name="Fogão",
            available=True,
            capacity=Decimal(4),
            capacity_unit="bocas",
        ),
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(equipment=equipment)
    )

    codes = {issue.code for issue in result.missing_information}
    assert "equipment_availability_unknown" in codes
    assert "equipment_capacity_unknown" in codes


RECIPE_REQUIREMENT_UNKNOWN_CODES = {
    "required_equipment": "recipe_equipment_requirements_unknown",
    "required_skills": "recipe_skill_requirements_unknown",
    "required_utilities": "recipe_utility_requirements_unknown",
}


def test_all_unknown_recipe_requirements_need_information_not_feasible() -> None:
    recipe = make_recipe(
        required_equipment=None,
        required_skills=None,
        required_utilities=None,
    )

    result = FeasibilityEvaluator().evaluate(recipe, make_kitchen())

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert result.blockers == ()
    assert {(issue.code, issue.subject) for issue in result.missing_information} == {
        (code, field) for field, code in RECIPE_REQUIREMENT_UNKNOWN_CODES.items()
    }
    # Batch planning still runs: unknown requirements do not hide known facts.
    assert result.required_batches == 1
    assert result.estimated_total_minutes == 90


def test_omitted_recipe_requirements_are_never_treated_as_feasible() -> None:
    # Simulates an incompletely researched recipe whose payload never
    # mentioned requirements at all.
    payload = make_recipe().model_dump(exclude=set(RECIPE_REQUIREMENT_UNKNOWN_CODES))
    recipe = Recipe.model_validate(payload)

    result = FeasibilityEvaluator().evaluate(recipe, make_kitchen())

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert {issue.code for issue in result.missing_information} == set(
        RECIPE_REQUIREMENT_UNKNOWN_CODES.values()
    )


def test_explicitly_empty_requirements_mean_no_requirements() -> None:
    recipe = make_recipe(
        required_equipment=(),
        required_skills=(),
        required_utilities=(),
    )
    # Kitchen inventory, skills and utilities are all unknown; that is
    # irrelevant because the recipe is confirmed to require none of them.
    kitchen = make_kitchen(
        equipment=None,
        equipment_inventory_complete=None,
        skills=None,
        gas_available=None,
        water_available=None,
    )

    result = FeasibilityEvaluator().evaluate(recipe, kitchen)

    assert result.status == FeasibilityStatus.FEASIBLE
    assert result.blockers == ()
    assert result.missing_information == ()


@pytest.mark.parametrize(
    ("field", "expected_code"),
    list(RECIPE_REQUIREMENT_UNKNOWN_CODES.items()),
)
def test_single_unknown_requirement_category_needs_information(
    field: str,
    expected_code: str,
) -> None:
    recipe = make_recipe(**{field: None})

    result = FeasibilityEvaluator().evaluate(recipe, make_kitchen())

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert result.blockers == ()
    assert len(result.missing_information) == 1
    issue = result.missing_information[0]
    assert issue.code == expected_code
    assert issue.subject == field


def test_populated_requirements_keep_deterministic_kitchen_validation() -> None:
    evaluator = FeasibilityEvaluator()
    recipe = make_recipe()
    kitchen_side_codes = {
        "equipment_inventory_unknown",
        "skill_inventory_unknown",
        "utility_availability_unknown",
    }

    unknown_kitchen = evaluator.evaluate(recipe, KitchenProfile())
    blocked = evaluator.evaluate(recipe, make_kitchen(skills=("arroz",)))

    # Confirmed recipe requirements are checked item by item against the
    # kitchen; no recipe-side "unknown requirements" issue is emitted.
    unknown_codes = {issue.code for issue in unknown_kitchen.missing_information}
    assert not unknown_codes & set(RECIPE_REQUIREMENT_UNKNOWN_CODES.values())
    assert [
        issue.subject
        for issue in unknown_kitchen.missing_information
        if issue.code in kitchen_side_codes
    ] == ["Panela de pressão", "Fogão", "galinhada", "gas", "water"]

    assert blocked.status == FeasibilityStatus.INFEASIBLE
    assert [issue.code for issue in blocked.blockers] == ["required_skill_missing"]
    assert evaluator.evaluate(recipe, make_kitchen(skills=("arroz",))) == blocked


def test_confirmed_blocker_wins_over_unknown_requirements_which_remain() -> None:
    recipe = make_recipe(
        required_equipment=None,
        required_skills=None,
        required_utilities=None,
    )

    result = FeasibilityEvaluator().evaluate(
        recipe, make_kitchen(daily_available_minutes=60)
    )

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert [issue.code for issue in result.blockers] == ["production_window_exceeded"]
    assert {issue.code for issue in result.missing_information} == set(
        RECIPE_REQUIREMENT_UNKNOWN_CODES.values()
    )


def test_unknown_category_does_not_mask_blocker_in_confirmed_category() -> None:
    recipe = make_recipe(required_equipment=None)

    result = FeasibilityEvaluator().evaluate(recipe, make_kitchen(gas_available=False))

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert [issue.code for issue in result.blockers] == ["utility_unavailable"]
    assert [issue.code for issue in result.missing_information] == [
        "recipe_equipment_requirements_unknown"
    ]


# --------------------------------------------------- phase 2 regressions


def test_unlisted_equipment_in_incomplete_inventory_is_unknown() -> None:
    kitchen = make_kitchen(equipment=(), equipment_inventory_complete=False)

    result = FeasibilityEvaluator().evaluate(make_recipe(), kitchen)

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert result.blockers == ()
    assert "equipment_not_reported" in {
        issue.code for issue in result.missing_information
    }


def test_unlisted_equipment_in_complete_inventory_is_unavailable() -> None:
    kitchen = make_kitchen(equipment=(), equipment_inventory_complete=True)

    result = FeasibilityEvaluator().evaluate(make_recipe(), kitchen)

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "equipment_missing" in {issue.code for issue in result.blockers}


def test_target_15_with_yield_8_requires_two_batches_and_surplus_confirmation() -> None:
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(target_servings_per_day=15)
    )

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert result.production_plan is not None
    assert result.production_plan.source_recipe_yield == 8
    assert result.production_plan.planned_batch_size == 8
    assert result.production_plan.batch_count == 2
    assert result.production_plan.total_produced_servings == 16
    assert result.production_plan.surplus_servings == 1
    assert {issue.code for issue in result.missing_information} >= {
        "production_surplus_unconfirmed",
        "multi_batch_duration_unconfirmed",
    }
    assert result.estimated_total_minutes is None


def test_confirmed_multi_batch_plan_uses_explicit_total_time() -> None:
    from sabor_da_maria_agent.domain.production import ProductionConfirmation

    confirmation = ProductionConfirmation(
        confirmed_batch_count=2,
        confirmed_source_recipe_yield=8,
        confirmed_target_daily_servings=15,
        production_minutes=210,
        user_confirmation="Confirmo dois lotes, total de 210 minutos.",
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(),
        make_kitchen(target_servings_per_day=15),
        confirmation,
    )

    assert result.status == FeasibilityStatus.FEASIBLE
    assert result.production_plan is not None
    assert result.production_plan.batch_count == 2
    assert result.production_plan.total_produced_servings == 16
    assert result.estimated_total_minutes == 210
    assert result.missing_information == ()


def test_production_confirmation_for_wrong_batch_count_is_ignored() -> None:
    from sabor_da_maria_agent.domain.production import ProductionConfirmation

    confirmation = ProductionConfirmation(
        confirmed_batch_count=1,
        confirmed_source_recipe_yield=8,
        confirmed_target_daily_servings=8,
        production_minutes=90,
        user_confirmation="Confirmo um lote.",
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(target_servings_per_day=15), confirmation
    )

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert {issue.code for issue in result.missing_information} >= {
        "production_confirmation_mismatch",
        "production_surplus_unconfirmed",
        "multi_batch_duration_unconfirmed",
    }
    assert result.estimated_total_minutes is None


def test_confirmed_total_time_can_resolve_unknown_source_duration() -> None:
    from sabor_da_maria_agent.domain.production import ProductionConfirmation

    confirmation = ProductionConfirmation(
        confirmed_batch_count=2,
        confirmed_source_recipe_yield=8,
        confirmed_target_daily_servings=15,
        production_minutes=200,
        user_confirmation="Confirmo dois lotes em 200 minutos no total.",
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(total_time_minutes=None),
        make_kitchen(target_servings_per_day=15),
        confirmation,
    )

    assert result.status == FeasibilityStatus.FEASIBLE
    assert result.estimated_total_minutes == 200
    assert "recipe_duration_unknown" not in {
        issue.code for issue in result.missing_information
    }


def test_confirmed_total_time_over_daily_window_is_infeasible() -> None:
    from sabor_da_maria_agent.domain.production import ProductionConfirmation

    confirmation = ProductionConfirmation(
        confirmed_batch_count=2,
        confirmed_source_recipe_yield=8,
        confirmed_target_daily_servings=15,
        production_minutes=300,
        user_confirmation="Confirmo dois lotes em 300 minutos no total.",
    )
    result = FeasibilityEvaluator().evaluate(
        make_recipe(),
        make_kitchen(target_servings_per_day=15, daily_available_minutes=240),
        confirmation,
    )

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "production_window_exceeded" in {issue.code for issue in result.blockers}


def test_refrigeration_capacity_is_checked_for_all_batches() -> None:
    from sabor_da_maria_agent.domain.kitchen import StorageKind, StorageRequirement
    from sabor_da_maria_agent.domain.production import ProductionConfirmation

    recipe = make_recipe(
        storage_requirements=(
            StorageRequirement(
                kind=StorageKind.REFRIGERATOR,
                volume_liters_per_batch=Decimal(10),
            ),
        )
    )
    confirmation = ProductionConfirmation(
        confirmed_batch_count=2,
        confirmed_source_recipe_yield=8,
        confirmed_target_daily_servings=15,
        production_minutes=200,
        user_confirmation="Confirmo dois lotes.",
    )
    kitchen = make_kitchen(
        target_servings_per_day=15,
        refrigerator_available=True,
        refrigerator_free_capacity_liters=Decimal(15),
    )

    result = FeasibilityEvaluator().evaluate(recipe, kitchen, confirmation)

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "storage_capacity_unresolved" in {
        issue.code for issue in result.missing_information
    }


def test_refrigeration_capacity_below_one_batch_is_a_blocker() -> None:
    from sabor_da_maria_agent.domain.kitchen import StorageKind, StorageRequirement

    recipe = make_recipe(
        storage_requirements=(
            StorageRequirement(
                kind=StorageKind.REFRIGERATOR,
                volume_liters_per_batch=Decimal(10),
            ),
        )
    )
    kitchen = make_kitchen(
        refrigerator_available=True,
        refrigerator_free_capacity_liters=Decimal(5),
    )

    result = FeasibilityEvaluator().evaluate(recipe, kitchen)

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "storage_capacity_insufficient" in {issue.code for issue in result.blockers}


def test_unknown_minimum_people_is_not_defaulted_to_one() -> None:
    result = FeasibilityEvaluator().evaluate(
        make_recipe(minimum_people=None), make_kitchen()
    )

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "recipe_minimum_people_unknown" in {
        issue.code for issue in result.missing_information
    }


def test_blender_does_not_satisfy_a_processor_requirement() -> None:
    recipe = make_recipe(required_equipment=(EquipmentRequirement(name="Processador"),))
    kitchen = make_kitchen(
        equipment=(KitchenEquipment(name="Liquidificador", available=True),),
        equipment_inventory_complete=True,
    )

    result = FeasibilityEvaluator().evaluate(recipe, kitchen)

    assert result.status == FeasibilityStatus.INFEASIBLE
    assert "equipment_missing" in {issue.code for issue in result.blockers}


def test_unknown_refrigerator_capacity_remains_unresolved() -> None:
    from sabor_da_maria_agent.domain.kitchen import StorageKind, StorageRequirement

    recipe = make_recipe(
        storage_requirements=(
            StorageRequirement(
                kind=StorageKind.REFRIGERATOR,
                volume_liters_per_batch=Decimal(10),
            ),
        )
    )
    kitchen = make_kitchen(refrigerator_available=True)

    result = FeasibilityEvaluator().evaluate(recipe, kitchen)

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "storage_capacity_unknown" in {
        issue.code for issue in result.missing_information
    }


def test_unknown_recipe_storage_requirements_are_not_treated_as_none() -> None:
    result = FeasibilityEvaluator().evaluate(
        make_recipe(storage_requirements=None), make_kitchen()
    )

    assert result.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "recipe_storage_requirements_unknown" in {
        issue.code for issue in result.missing_information
    }


# ------------------------------------- Staff audit: unknown versus unavailable


def test_skill_unreported_does_not_mean_unavailable() -> None:
    candidate = make_recipe()
    partial = FeasibilityEvaluator().evaluate(
        candidate, make_kitchen(skills=("arroz",), skills_inventory_complete=False)
    )
    assert partial.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "skill_not_reported" in {e.code for e in partial.missing_information}
    confirmed = FeasibilityEvaluator().evaluate(
        candidate, make_kitchen(skills=("arroz",), skills_inventory_complete=True)
    )
    assert confirmed.status == FeasibilityStatus.INFEASIBLE
    assert "required_skill_missing" in {e.code for e in confirmed.blockers}


def test_one_required_person_cannot_be_assumed_available() -> None:
    assessment = FeasibilityEvaluator().evaluate(
        make_recipe(minimum_people=1), make_kitchen(available_people=None)
    )
    assert assessment.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "available_people_unknown" in {
        i.code for i in assessment.missing_information
    }


def test_direct_unbound_confirmation_is_not_reusable() -> None:
    from sabor_da_maria_agent.domain.production import ProductionConfirmation

    unbound = ProductionConfirmation(
        confirmed_batch_count=2,
        production_minutes=200,
        user_confirmation="Confirmo 2 lotes.",
    )
    assessment = FeasibilityEvaluator().evaluate(
        make_recipe(), make_kitchen(target_servings_per_day=15), unbound
    )
    assert assessment.status == FeasibilityStatus.NEEDS_INFORMATION
    assert "production_confirmation_mismatch" in {
        i.code for i in assessment.missing_information
    }
