"""Deterministic operational feasibility evaluation for recipe candidates."""

from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sabor_da_maria_agent.domain.kitchen import (
    EquipmentAvailability,
    KitchenProfile,
    UtilityKind,
    comparison_key,
)
from sabor_da_maria_agent.domain.production import (
    ProductionConfirmation,
    ProductionPlan,
    build_production_plan,
)
from sabor_da_maria_agent.domain.recipe import Recipe


class FeasibilityStatus(StrEnum):
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    NEEDS_INFORMATION = "needs_information"


class FeasibilityIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    subject: str | None = None


class FeasibilityAssessment(BaseModel):
    """Auditable result; blockers and unknowns are never collapsed together.

    ``required_batches`` mirrors ``production_plan.batch_count``.
    ``estimated_total_minutes`` is only set when the duration of the whole
    plan is actually known: the source time for a single batch, or the time
    Dona Maria confirmed for the plan. It is never ``batches x time``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recipe_id: str
    status: FeasibilityStatus
    target_servings: int | None = None
    required_batches: int | None = None
    estimated_total_minutes: int | None = None
    production_plan: ProductionPlan | None = None
    blockers: tuple[FeasibilityIssue, ...] = ()
    missing_information: tuple[FeasibilityIssue, ...] = ()
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_status_consistency(self) -> "FeasibilityAssessment":
        if self.status == FeasibilityStatus.FEASIBLE:
            if self.blockers or self.missing_information:
                raise ValueError(
                    "A feasible assessment cannot contain blockers or unknowns."
                )
        elif self.status == FeasibilityStatus.INFEASIBLE:
            if not self.blockers:
                raise ValueError("An infeasible assessment must contain a blocker.")
        elif self.status == FeasibilityStatus.NEEDS_INFORMATION and (
            self.blockers or not self.missing_information
        ):
            raise ValueError(
                "Needs-information requires unknowns and cannot contain blockers."
            )
        if self.production_plan is not None and (
            self.required_batches != self.production_plan.batch_count
            or self.target_servings != self.production_plan.target_daily_servings
        ):
            raise ValueError("Batch summary must match the production plan.")
        return self


def _issue(code: str, message: str, subject: str | None = None) -> FeasibilityIssue:
    return FeasibilityIssue(code=code, message=message, subject=subject)


class FeasibilityEvaluator:
    """Evaluate recipe requirements against facts explicitly known about a kitchen.

    - Production is planned in whole, unscaled batches (``ProductionPlan``).
      Surplus servings require Dona Maria's confirmation for that batch count.
      Without a daily target there is no plan, hence never ``feasible``.
    - Time: a single batch uses the source duration. A multi-batch plan needs
      a confirmed total duration; it is never extrapolated linearly. If even
      one batch exceeds the available time, the recipe is infeasible.
    - Equipment: an item missing from an inventory that is not confirmed
      complete is *unknown* (needs information), never unavailable. Names
      are matched exactly; no substitute equipment is assumed.
    - Cold storage: compared with the free capacity Dona Maria reported.
      One batch over capacity is a blocker; all batches within capacity is
      adequate; anything in between is unresolved (needs information).
    - Recipe requirements that have not been researched (``None``) are
      reported as missing information; only an explicitly empty tuple means
      the recipe requires nothing in that category. ``minimum_people`` is
      never defaulted to 1.

    ``FEASIBLE`` is an operational verdict only. It says nothing about pantry
    completeness, purchase approval or costing readiness.
    """

    _UTILITY_FIELDS: ClassVar[dict[UtilityKind, str]] = {
        UtilityKind.GAS: "gas_available",
        UtilityKind.ELECTRICITY: "electricity_available",
        UtilityKind.WATER: "water_available",
    }

    def evaluate(
        self,
        recipe: Recipe,
        kitchen: KitchenProfile,
        production_confirmation: ProductionConfirmation | None = None,
    ) -> FeasibilityAssessment:
        blockers: list[FeasibilityIssue] = []
        missing: list[FeasibilityIssue] = []
        warnings: list[str] = []

        plan, confirmation = self._plan_production(
            recipe, kitchen, production_confirmation, missing
        )
        estimated_total_minutes = self._evaluate_time(
            recipe, kitchen, plan, confirmation, blockers, missing
        )
        self._evaluate_equipment(recipe, kitchen, blockers, missing)
        self._evaluate_skills(recipe, kitchen, blockers, missing)
        self._evaluate_utilities(recipe, kitchen, blockers, missing)
        self._evaluate_people(recipe, kitchen, blockers, missing)
        self._evaluate_storage(recipe, kitchen, plan, blockers, missing)

        if recipe.unquantified_ingredients:
            warnings.append(
                "Ingredient quantities are missing for: "
                + ", ".join(recipe.unquantified_ingredients)
                + ". Complete quantities before calculating a complete CMV."
            )

        # A confirmed blocker takes precedence over unknowns: the recipe is
        # already impossible under the known constraints, even if other facts
        # remain to be collected. Unknowns are still returned for auditability.
        if blockers:
            status = FeasibilityStatus.INFEASIBLE
        elif missing:
            status = FeasibilityStatus.NEEDS_INFORMATION
        else:
            status = FeasibilityStatus.FEASIBLE

        return FeasibilityAssessment(
            recipe_id=recipe.recipe_id,
            status=status,
            target_servings=kitchen.target_servings_per_day,
            required_batches=plan.batch_count if plan is not None else None,
            estimated_total_minutes=estimated_total_minutes,
            production_plan=plan,
            blockers=tuple(blockers),
            missing_information=tuple(missing),
            warnings=tuple(warnings),
        )

    # ---------------------------------------------------------- production
    @staticmethod
    def _plan_production(
        recipe: Recipe,
        kitchen: KitchenProfile,
        confirmation: ProductionConfirmation | None,
        missing: list[FeasibilityIssue],
    ) -> tuple[ProductionPlan | None, ProductionConfirmation | None]:
        """Return the plan, and the confirmation only if it applies to it."""
        target = kitchen.target_servings_per_day
        if target is None:
            missing.append(
                _issue(
                    "target_servings_unknown",
                    "Daily production target has not been provided; the "
                    "production plan cannot be established.",
                    "target_servings_per_day",
                )
            )
            return None, None

        plan = build_production_plan(recipe, target)
        applies = (
            confirmation is not None
            and confirmation.confirmed_batch_count == plan.batch_count
            and confirmation.confirmed_target_daily_servings
            == plan.target_daily_servings
            and confirmation.confirmed_source_recipe_yield == plan.source_recipe_yield
        )
        if confirmation is not None and not applies:
            missing.append(
                _issue(
                    "production_confirmation_mismatch",
                    "The recorded confirmation is not bound to the current "
                    f"plan (yield={plan.source_recipe_yield}, "
                    f"target={plan.target_daily_servings}, "
                    f"batches={plan.batch_count}). Ask Dona Maria again.",
                    "production_confirmation",
                )
            )
        if plan.surplus_servings > 0 and not applies:
            missing.append(
                _issue(
                    "production_surplus_unconfirmed",
                    f"The recipe yields {plan.planned_batch_size} servings per "
                    f"batch. Reaching {plan.target_daily_servings} servings takes "
                    f"{plan.batch_count} whole batches = "
                    f"{plan.total_produced_servings} servings "
                    f"({plan.surplus_servings} more than the target). Partial "
                    "batches are not assumed. Dona Maria must confirm this plan.",
                    "production_plan",
                )
            )
        return plan, confirmation if applies else None

    @staticmethod
    def _evaluate_time(
        recipe: Recipe,
        kitchen: KitchenProfile,
        plan: ProductionPlan | None,
        confirmation: ProductionConfirmation | None,
        blockers: list[FeasibilityIssue],
        missing: list[FeasibilityIssue],
    ) -> int | None:
        batch_minutes = recipe.total_time_minutes
        available = kitchen.daily_available_minutes
        has_confirmed_total = (
            confirmation is not None and confirmation.production_minutes is not None
        )
        if batch_minutes is None and not has_confirmed_total:
            missing.append(
                _issue(
                    "recipe_duration_unknown",
                    "Recipe duration per batch has not been established, and no "
                    "total production duration has been confirmed.",
                    "total_time_minutes",
                )
            )

        estimated: int | None = None
        if has_confirmed_total:
            # A direct confirmation for the complete plan is sufficient even
            # when the source does not state a per-batch duration.
            assert confirmation is not None
            estimated = confirmation.production_minutes
        elif plan is not None and plan.batch_count == 1:
            estimated = batch_minutes
        elif plan is not None:
            missing.append(
                _issue(
                    "multi_batch_duration_unconfirmed",
                    f"The plan needs {plan.batch_count} batches. Cooking time "
                    "is not assumed to scale linearly; Dona Maria must confirm "
                    "the total time for all batches.",
                    "production_minutes",
                )
            )

        if available is None:
            missing.append(
                _issue(
                    "available_time_unknown",
                    "Daily kitchen time availability has not been provided.",
                    "daily_available_minutes",
                )
            )
        elif estimated is not None:
            if estimated > available:
                blockers.append(
                    _issue(
                        "production_window_exceeded",
                        f"Estimated production requires {estimated} minutes, "
                        f"but only {available} minutes are available.",
                        "daily_available_minutes",
                    )
                )
        elif batch_minutes is not None and batch_minutes > available:
            # Even with any parallelism the plan takes at least one batch.
            blockers.append(
                _issue(
                    "production_window_exceeded",
                    f"A single batch already requires {batch_minutes} minutes, "
                    f"but only {available} minutes are available.",
                    "daily_available_minutes",
                )
            )
        return estimated

    # ----------------------------------------------------------- equipment
    @staticmethod
    def _evaluate_equipment(
        recipe: Recipe,
        kitchen: KitchenProfile,
        blockers: list[FeasibilityIssue],
        missing: list[FeasibilityIssue],
    ) -> None:
        if recipe.required_equipment is None:
            missing.append(
                _issue(
                    "recipe_equipment_requirements_unknown",
                    "Equipment required by the recipe has not been "
                    "researched or confirmed.",
                    "required_equipment",
                )
            )
            return
        if not recipe.required_equipment:
            return

        if kitchen.equipment is None:
            for requirement in recipe.required_equipment:
                missing.append(
                    _issue(
                        "equipment_inventory_unknown",
                        "Availability of required equipment "
                        f"'{requirement.name}' is unknown.",
                        requirement.name,
                    )
                )
            return

        inventory = kitchen.equipment_by_name()
        for requirement in recipe.required_equipment:
            equipment = inventory.get(comparison_key(requirement.name))
            if equipment is None:
                if (
                    kitchen.equipment_availability(requirement.name)
                    == EquipmentAvailability.UNAVAILABLE
                ):
                    blockers.append(
                        _issue(
                            "equipment_missing",
                            f"Required equipment '{requirement.name}' is not in "
                            "the kitchen inventory, which Dona Maria confirmed "
                            "is complete.",
                            requirement.name,
                        )
                    )
                else:
                    missing.append(
                        _issue(
                            "equipment_not_reported",
                            f"Required equipment '{requirement.name}' was not "
                            "reported and the inventory is not confirmed "
                            "complete. Ask whether it is available; no substitute "
                            "equipment is assumed.",
                            requirement.name,
                        )
                    )
                continue

            availability = equipment.availability
            if availability == EquipmentAvailability.UNAVAILABLE:
                blockers.append(
                    _issue(
                        "equipment_unavailable",
                        f"Required equipment '{requirement.name}' is unavailable.",
                        requirement.name,
                    )
                )
                continue
            if availability == EquipmentAvailability.UNKNOWN:
                missing.append(
                    _issue(
                        "equipment_availability_unknown",
                        f"Availability of '{requirement.name}' has not been confirmed.",
                        requirement.name,
                    )
                )

            if equipment.quantity < requirement.minimum_quantity:
                blockers.append(
                    _issue(
                        "equipment_quantity_insufficient",
                        f"'{requirement.name}' requires "
                        f"{requirement.minimum_quantity} units, but "
                        f"{equipment.quantity} are available.",
                        requirement.name,
                    )
                )

            if requirement.minimum_capacity is not None:
                if equipment.capacity is None or equipment.capacity_unit is None:
                    missing.append(
                        _issue(
                            "equipment_capacity_unknown",
                            f"Capacity of '{requirement.name}' has not been provided.",
                            requirement.name,
                        )
                    )
                elif equipment.capacity_unit != requirement.capacity_unit:
                    missing.append(
                        _issue(
                            "equipment_capacity_unit_incompatible",
                            f"Capacity unit for '{requirement.name}' is "
                            f"'{equipment.capacity_unit}', expected "
                            f"'{requirement.capacity_unit}'.",
                            requirement.name,
                        )
                    )
                elif equipment.capacity < requirement.minimum_capacity:
                    blockers.append(
                        _issue(
                            "equipment_capacity_insufficient",
                            f"'{requirement.name}' capacity is "
                            f"{equipment.capacity} {equipment.capacity_unit}; "
                            f"at least {requirement.minimum_capacity} "
                            f"{requirement.capacity_unit} is required.",
                            requirement.name,
                        )
                    )

    @staticmethod
    def _evaluate_skills(
        recipe: Recipe,
        kitchen: KitchenProfile,
        blockers: list[FeasibilityIssue],
        missing: list[FeasibilityIssue],
    ) -> None:
        if recipe.required_skills is None:
            missing.append(
                _issue(
                    "recipe_skill_requirements_unknown",
                    "Cooking skills required by the recipe have not been "
                    "researched or confirmed.",
                    "required_skills",
                )
            )
            return
        if not recipe.required_skills:
            return
        if kitchen.skills is None:
            for skill in recipe.required_skills:
                missing.append(
                    _issue(
                        "skill_inventory_unknown",
                        f"Experience with '{skill}' has not been confirmed.",
                        skill,
                    )
                )
            return
        known_skills = {comparison_key(skill) for skill in kitchen.skills}
        for skill in recipe.required_skills:
            if comparison_key(skill) not in known_skills:
                if kitchen.skills_inventory_complete:
                    blockers.append(
                        _issue(
                            "required_skill_missing",
                            f"Required cooking skill '{skill}' is absent from "
                            "the confirmed complete skills inventory.",
                            skill,
                        )
                    )
                else:
                    missing.append(
                        _issue(
                            "skill_not_reported",
                            f"Ask whether Dona Maria has experience with "
                            f"'{skill}'; the skills list is not exhaustive.",
                            skill,
                        )
                    )

    def _evaluate_utilities(
        self,
        recipe: Recipe,
        kitchen: KitchenProfile,
        blockers: list[FeasibilityIssue],
        missing: list[FeasibilityIssue],
    ) -> None:
        if recipe.required_utilities is None:
            missing.append(
                _issue(
                    "recipe_utility_requirements_unknown",
                    "Utilities required by the recipe have not been "
                    "researched or confirmed.",
                    "required_utilities",
                )
            )
            return
        for utility in recipe.required_utilities:
            field_name = self._UTILITY_FIELDS[utility]
            available = getattr(kitchen, field_name)
            if available is None:
                missing.append(
                    _issue(
                        "utility_availability_unknown",
                        f"Availability of utility '{utility.value}' "
                        "has not been confirmed.",
                        utility.value,
                    )
                )
            elif not available:
                blockers.append(
                    _issue(
                        "utility_unavailable",
                        f"Required utility '{utility.value}' is unavailable.",
                        utility.value,
                    )
                )

    # -------------------------------------------------------------- people
    @staticmethod
    def _evaluate_people(
        recipe: Recipe,
        kitchen: KitchenProfile,
        blockers: list[FeasibilityIssue],
        missing: list[FeasibilityIssue],
    ) -> None:
        if recipe.minimum_people is None:
            missing.append(
                _issue(
                    "recipe_minimum_people_unknown",
                    "The minimum number of people needed to produce the recipe "
                    "was not stated by the source nor confirmed by Dona Maria.",
                    "minimum_people",
                )
            )
            return
        if kitchen.available_people is None:
            missing.append(
                _issue(
                    "available_people_unknown",
                    "Number of people available for production is unknown.",
                    "available_people",
                )
            )
        elif kitchen.available_people < recipe.minimum_people:
            blockers.append(
                _issue(
                    "insufficient_people",
                    f"Recipe requires {recipe.minimum_people} people, "
                    f"but only {kitchen.available_people} are available.",
                    "available_people",
                )
            )

    # ------------------------------------------------------------- storage
    @staticmethod
    def _evaluate_storage(
        recipe: Recipe,
        kitchen: KitchenProfile,
        plan: ProductionPlan | None,
        blockers: list[FeasibilityIssue],
        missing: list[FeasibilityIssue],
    ) -> None:
        if recipe.storage_requirements is None:
            missing.append(
                _issue(
                    "recipe_storage_requirements_unknown",
                    "Cold-storage needs (refrigerator/freezer) of the recipe "
                    "have not been researched or confirmed.",
                    "storage_requirements",
                )
            )
            return
        for requirement in recipe.storage_requirements:
            kind = requirement.kind.value
            available, capacity = kitchen.storage(requirement.kind)
            if available is None:
                missing.append(
                    _issue(
                        "storage_availability_unknown",
                        f"Availability of a {kind} has not been confirmed.",
                        kind,
                    )
                )
                continue
            if not available:
                blockers.append(
                    _issue(
                        "storage_unavailable",
                        f"The recipe requires a {kind}, which is unavailable.",
                        kind,
                    )
                )
                continue
            volume = requirement.volume_liters_per_batch
            if volume is None:
                missing.append(
                    _issue(
                        "storage_volume_unknown",
                        f"The {kind} volume one batch occupies is unknown; it is "
                        "never inferred from the number of servings.",
                        kind,
                    )
                )
                continue
            if capacity is None:
                missing.append(
                    _issue(
                        "storage_capacity_unknown",
                        f"Free {kind} capacity has not been provided.",
                        kind,
                    )
                )
                continue
            if volume > capacity:
                blockers.append(
                    _issue(
                        "storage_capacity_insufficient",
                        f"One batch needs {volume} L of {kind} space, but only "
                        f"{capacity} L are free.",
                        kind,
                    )
                )
                continue
            if plan is None:
                missing.append(
                    _issue(
                        "storage_capacity_unresolved",
                        f"Free {kind} space fits one batch, but the number of "
                        "batches is unknown.",
                        kind,
                    )
                )
                continue
            all_batches = volume * Decimal(plan.batch_count)
            if all_batches > capacity:
                missing.append(
                    _issue(
                        "storage_capacity_unresolved",
                        f"Free {kind} space ({capacity} L) fits one batch "
                        f"({volume} L) but not all {plan.batch_count} batches "
                        f"stored together ({all_batches} L). Confirm how the "
                        "batches are stored.",
                        kind,
                    )
                )
