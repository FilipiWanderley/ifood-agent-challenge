"""Whole-batch production planning derived from the source recipe yield.

A ``ProductionPlan`` is the single source of truth for *how much* is
produced. Ingredient checks, purchase quantities, the R$ 80 budget check and
the final CMV all consume the same plan, so the system can never report two
batches while checking stock for one.

Rules (deliberately conservative):

- A batch is the source recipe exactly as stated (``planned_batch_size`` is
  the source yield). Recipes are never scaled to fractional or partial
  batches, because the source does not prove such scaling works.
- ``batch_count`` is the minimum number of whole batches whose total yield
  covers the daily target. 12 servings never satisfy a target of 15.
- Any surplus (``total_produced_servings - target``) is reported explicitly
  and must be confirmed by Dona Maria for this exact batch count; it is
  never silently accepted.
- Cooking time is never assumed to scale linearly with batches. The time of
  a multi-batch plan must be confirmed explicitly.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sabor_da_maria_agent.domain.recipe import Recipe

MAX_CONFIRMATION_LENGTH = 2000


class ProductionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProductionConfirmation(ProductionModel):
    """Dona Maria's explicit answers about one specific production plan.

    A confirmation applies only to its exact source yield, daily target and
    batch count. The application service binds the yield and target when Dona
    Maria confirms; changing even the target within the same batch count
    requires a new confirmation of surplus and production duration.
    """

    confirmed_batch_count: int = Field(
        gt=0,
        description="Whole batches Dona Maria agreed to produce (accepts surplus).",
    )
    production_minutes: int | None = Field(
        default=None,
        gt=0,
        description="Total minutes she confirmed for ALL batches of the plan.",
    )
    # Bound by PlanningService at registration, NOT trusted from the LLM.
    # An unbound confirmation is never proof of a specific plan.
    confirmed_target_daily_servings: int | None = Field(default=None, gt=0)
    confirmed_source_recipe_yield: int | None = Field(default=None, gt=0)
    user_confirmation: str = Field(min_length=1, max_length=MAX_CONFIRMATION_LENGTH)

    @field_validator("user_confirmation")
    @classmethod
    def normalize_confirmation(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("user_confirmation must quote Dona Maria.")
        return normalized


class PlannedIngredient(ProductionModel):
    """Requirement of one ingredient for the complete production plan.

    ``quantity_per_batch`` and ``unit`` are the original recipe values.
    ``total_quantity`` is ``quantity_per_batch * batch_count`` in the same
    original unit, or ``None`` when the recipe quantity is unknown.
    """

    name: str = Field(min_length=1)
    quantity_per_batch: Decimal | None
    unit: str | None
    batch_count: int = Field(gt=0)
    total_quantity: Decimal | None

    @model_validator(mode="after")
    def validate_total(self) -> "PlannedIngredient":
        if (self.quantity_per_batch is None) != (self.total_quantity is None):
            raise ValueError("Total quantity is known exactly when the batch one is.")
        if (
            self.quantity_per_batch is not None
            and self.total_quantity != self.quantity_per_batch * self.batch_count
        ):
            raise ValueError("Total quantity must equal per-batch quantity x batches.")
        return self


class ProductionPlan(ProductionModel):
    """Explicit, internally consistent daily production plan."""

    source_recipe_yield: int = Field(gt=0)
    target_daily_servings: int = Field(gt=0)
    planned_batch_size: int = Field(gt=0)
    batch_count: int = Field(gt=0)
    total_produced_servings: int = Field(gt=0)
    surplus_servings: int = Field(ge=0)
    ingredients: tuple[PlannedIngredient, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_consistency(self) -> "ProductionPlan":
        if self.planned_batch_size != self.source_recipe_yield:
            raise ValueError("Batches are never scaled: batch size must equal yield.")
        if self.total_produced_servings != self.batch_count * self.planned_batch_size:
            raise ValueError("Total servings must equal batches x batch size.")
        if self.total_produced_servings < self.target_daily_servings:
            raise ValueError("The plan must cover the daily target.")
        if (
            self.batch_count - 1
        ) * self.planned_batch_size >= self.target_daily_servings:
            raise ValueError("The plan must use the minimum number of batches.")
        if self.surplus_servings != (
            self.total_produced_servings - self.target_daily_servings
        ):
            raise ValueError("Surplus must equal total servings minus target.")
        if any(item.batch_count != self.batch_count for item in self.ingredients):
            raise ValueError("Every ingredient must use the plan batch count.")
        return self


def required_batches(target_servings: int, recipe_yield: int) -> int:
    """Minimum whole batches whose total yield covers ``target_servings``."""
    if target_servings <= 0 or recipe_yield <= 0:
        raise ValueError("Target servings and recipe yield must be positive.")
    return (target_servings + recipe_yield - 1) // recipe_yield


def build_production_plan(recipe: Recipe, target_servings: int) -> ProductionPlan:
    """Plan whole batches of the unmodified source recipe for the target."""
    batches = required_batches(target_servings, recipe.servings)
    total = batches * recipe.servings
    return ProductionPlan(
        source_recipe_yield=recipe.servings,
        target_daily_servings=target_servings,
        planned_batch_size=recipe.servings,
        batch_count=batches,
        total_produced_servings=total,
        surplus_servings=total - target_servings,
        ingredients=tuple(
            PlannedIngredient(
                name=item.name,
                quantity_per_batch=item.quantity,
                unit=item.unit,
                batch_count=batches,
                total_quantity=(
                    item.quantity * batches if item.quantity is not None else None
                ),
            )
            for item in recipe.ingredients
        ),
    )
