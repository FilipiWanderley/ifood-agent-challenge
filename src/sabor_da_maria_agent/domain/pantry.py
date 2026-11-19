from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sabor_da_maria_agent.domain.units import MeasureDimension


class IngredientCost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    stock_quantity: Decimal = Field(gt=0)
    stock_unit: str = Field(min_length=1)
    stock_base_quantity: Decimal = Field(gt=0)
    purchase_quantity: Decimal = Field(gt=0)
    purchase_unit: str = Field(min_length=1)
    purchase_base_quantity: Decimal = Field(gt=0)
    purchase_total: Decimal = Field(gt=0)
    base_unit: str = Field(min_length=1)
    dimension: MeasureDimension
    unit_cost: Decimal = Field(gt=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Ingredient name cannot be empty.")
        return normalized


class PantryCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ingredients: tuple[IngredientCost, ...]
    historical_purchase_total: Decimal = Field(ge=0)
    remaining_budget: Decimal = Field(default=Decimal("80.00"), ge=0)

    @field_validator("ingredients")
    @classmethod
    def validate_unique_ingredients(
        cls,
        ingredients: tuple[IngredientCost, ...],
    ) -> tuple[IngredientCost, ...]:
        if not ingredients:
            raise ValueError("Pantry catalog cannot be empty.")

        names = [ingredient.name.casefold() for ingredient in ingredients]

        if len(names) != len(set(names)):
            raise ValueError("Pantry catalog contains duplicate ingredients.")

        return ingredients