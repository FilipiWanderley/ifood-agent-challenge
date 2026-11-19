from collections.abc import Sequence
from decimal import ROUND_CEILING, Decimal
from unicodedata import normalize

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sabor_da_maria_agent.domain.pantry import IngredientCost, PantryCatalog
from sabor_da_maria_agent.domain.units import (
    MeasureDimension,
    NormalizedMeasure,
    normalize_measure,
)

CENT = Decimal("0.01")


class CostingError(ValueError):
    pass


class MissingSupplementalQuoteError(CostingError):
    pass


class IncompatibleMeasurementError(CostingError):
    pass


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecipeIngredientRequirement(FrozenModel):
    name: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1)

    @field_validator("name", "unit")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value cannot be empty.")
        return normalized

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("Quantity must be positive and finite.")
        return value


class SupplementalPurchaseQuote(FrozenModel):
    name: str = Field(min_length=1)
    package_quantity: Decimal = Field(gt=0)
    package_unit: str = Field(min_length=1)
    package_price: Decimal = Field(gt=0)
    source: str = Field(min_length=1)

    @field_validator("name", "package_unit", "source")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value cannot be empty.")
        return normalized

    @field_validator("package_quantity", "package_price")
    @classmethod
    def validate_positive_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("Value must be positive and finite.")
        return value


class IngredientCostBreakdown(FrozenModel):
    name: str
    base_unit: str
    dimension: MeasureDimension
    required_quantity: Decimal = Field(gt=0)
    stock_available_quantity: Decimal = Field(ge=0)
    stock_used_quantity: Decimal = Field(ge=0)
    supplemental_quantity: Decimal = Field(ge=0)
    historical_unit_cost: Decimal | None = Field(default=None, gt=0)
    supplemental_unit_cost: Decimal | None = Field(default=None, gt=0)
    consumed_cost: Decimal = Field(ge=0)
    purchase_package_count: int = Field(ge=0)
    purchase_base_quantity: Decimal = Field(ge=0)
    purchase_total: Decimal = Field(ge=0)
    remaining_stock_quantity: Decimal = Field(ge=0)
    purchase_source: str | None = None


class RecipeCost(FrozenModel):
    recipe_name: str = Field(min_length=1)
    servings: int = Field(gt=0)
    lines: tuple[IngredientCostBreakdown, ...]
    total_cmv: Decimal = Field(gt=0)
    cmv_per_portion: Decimal = Field(gt=0)
    supplemental_purchase_total: Decimal = Field(ge=0)
    budget_limit: Decimal = Field(ge=0)
    budget_remaining_after_purchase: Decimal
    within_budget: bool


class PricingScenario(FrozenModel):
    target_margin_on_gross_price: Decimal
    selling_price: Decimal = Field(gt=0)
    platform_fee: Decimal = Field(ge=0)
    net_revenue: Decimal = Field(ge=0)
    estimated_profit: Decimal
    realized_margin_on_gross_price: Decimal


class PricingAnalysis(FrozenModel):
    platform_fee_rate: Decimal
    break_even_price: Decimal = Field(gt=0)
    scenarios: tuple[PricingScenario, ...]


def _normalize_name(value: str) -> str:
    return " ".join(normalize("NFKC", value).casefold().split())


def _normalize_measure(
    quantity: Decimal,
    unit: str,
    ingredient_name: str,
) -> NormalizedMeasure:
    try:
        return normalize_measure(quantity, unit)
    except ValueError as exc:
        raise CostingError(
            f"Invalid measurement for {ingredient_name!r}: {exc}"
        ) from exc




class RecipeCostCalculator:
    def calculate(
        self,
        recipe_name: str,
        requirements: Sequence[RecipeIngredientRequirement],
        catalog: PantryCatalog,
        supplemental_quotes: Sequence[SupplementalPurchaseQuote] = (),
        servings: int = 1,
    ) -> RecipeCost:
        if not recipe_name.strip():
            raise CostingError("Recipe name cannot be empty.")

        if isinstance(servings, bool) or not isinstance(servings, int) or servings <= 0:
            raise CostingError("Servings must be a positive integer.")

        if not requirements:
            raise CostingError("Recipe must contain ingredients.")

        catalog_by_key: dict[str, IngredientCost] = {}

        for catalog_ingredient in catalog.ingredients:
            key = _normalize_name(catalog_ingredient.name)

            if key in catalog_by_key:
                raise CostingError(
                    f"Ambiguous catalog ingredient: {catalog_ingredient.name!r}."
                )

            catalog_by_key[key] = catalog_ingredient

        quotes_by_key: dict[str, SupplementalPurchaseQuote] = {}

        for supplemental_quote in supplemental_quotes:
            key = _normalize_name(supplemental_quote.name)

            if key in quotes_by_key:
                raise CostingError(
                    f"Duplicate purchase quote: {supplemental_quote.name!r}."
                )

            quotes_by_key[key] = supplemental_quote

        aggregated: dict[
            str,
            tuple[str, Decimal, str, MeasureDimension],
        ] = {}

        for requirement in requirements:
            measure = _normalize_measure(
                requirement.quantity,
                requirement.unit,
                requirement.name,
            )
            key = _normalize_name(requirement.name)

            if key in aggregated:
                name, quantity, unit, dimension = aggregated[key]

                if unit != measure.unit or dimension != measure.dimension:
                    raise IncompatibleMeasurementError(
                        f"Incompatible recipe measurements for {requirement.name!r}."
                    )

                aggregated[key] = (
                    name,
                    quantity + measure.quantity,
                    unit,
                    dimension,
                )
            else:
                aggregated[key] = (
                    requirement.name,
                    measure.quantity,
                    measure.unit,
                    measure.dimension,
                )

        unused_quotes = set(quotes_by_key).difference(aggregated)

        if unused_quotes:
            raise CostingError(
                f"Quotes supplied for unused ingredients: {sorted(unused_quotes)}."
            )

        lines: list[IngredientCostBreakdown] = []

        for key, (
            name,
            required_quantity,
            base_unit,
            dimension,
        ) in aggregated.items():
            ingredient = catalog_by_key.get(key)
            quote = quotes_by_key.get(key)

            if ingredient is not None:
                if (
                    ingredient.base_unit != base_unit
                    or ingredient.dimension != dimension
                ):
                    raise IncompatibleMeasurementError(
                        f"Recipe and pantry measurements are incompatible for {name!r}."
                    )

                stock_available = ingredient.stock_base_quantity
                historical_unit_cost = ingredient.unit_cost
            else:
                stock_available = Decimal(0)
                historical_unit_cost = None

            stock_used = min(stock_available, required_quantity)
            supplemental_quantity = required_quantity - stock_used

            supplemental_unit_cost = None
            package_count = 0
            purchase_base_quantity = Decimal(0)
            purchase_total = Decimal(0)
            purchase_source = None

            if supplemental_quantity > 0:
                if quote is None:
                    raise MissingSupplementalQuoteError(
                        f"A verifiable purchase quote is required for "
                        f"{name!r}. Required quantity: "
                        f"{supplemental_quantity} {base_unit}."
                    )

                package_measure = _normalize_measure(
                    quote.package_quantity,
                    quote.package_unit,
                    quote.name,
                )

                if (
                    package_measure.unit != base_unit
                    or package_measure.dimension != dimension
                ):
                    raise IncompatibleMeasurementError(
                        f"Purchase quote measurement is incompatible for {name!r}."
                    )

                supplemental_unit_cost = quote.package_price / package_measure.quantity

                package_count = int(
                    (
                        supplemental_quantity / package_measure.quantity
                    ).to_integral_value(rounding=ROUND_CEILING)
                )

                purchase_base_quantity = (
                    Decimal(package_count) * package_measure.quantity
                )
                purchase_total = Decimal(package_count) * quote.package_price
                purchase_source = quote.source

            stock_consumed_cost = (
                stock_used * historical_unit_cost
                if historical_unit_cost is not None
                else Decimal(0)
            )

            supplemental_consumed_cost = (
                supplemental_quantity * supplemental_unit_cost
                if supplemental_unit_cost is not None
                else Decimal(0)
            )

            consumed_cost = stock_consumed_cost + supplemental_consumed_cost

            remaining_stock = (
                stock_available
                - stock_used
                + purchase_base_quantity
                - supplemental_quantity
            )

            lines.append(
                IngredientCostBreakdown(
                    name=name,
                    base_unit=base_unit,
                    dimension=dimension,
                    required_quantity=required_quantity,
                    stock_available_quantity=stock_available,
                    stock_used_quantity=stock_used,
                    supplemental_quantity=supplemental_quantity,
                    historical_unit_cost=historical_unit_cost,
                    supplemental_unit_cost=supplemental_unit_cost,
                    consumed_cost=consumed_cost,
                    purchase_package_count=package_count,
                    purchase_base_quantity=purchase_base_quantity,
                    purchase_total=purchase_total,
                    remaining_stock_quantity=remaining_stock,
                    purchase_source=purchase_source,
                )
            )

        total_cmv = sum(
            (line.consumed_cost for line in lines),
            start=Decimal(0),
        )

        supplemental_purchase_total = sum(
            (line.purchase_total for line in lines),
            start=Decimal(0),
        )

        budget_remaining = catalog.remaining_budget - supplemental_purchase_total

        return RecipeCost(
            recipe_name=recipe_name.strip(),
            servings=servings,
            lines=tuple(lines),
            total_cmv=total_cmv,
            cmv_per_portion=total_cmv / Decimal(servings),
            supplemental_purchase_total=supplemental_purchase_total,
            budget_limit=catalog.remaining_budget,
            budget_remaining_after_purchase=budget_remaining,
            within_budget=budget_remaining >= 0,
        )


class PricingPolicy(FrozenModel):
    platform_fee_rate: Decimal = Field(
        default=Decimal("0.10"),
        ge=0,
        lt=1,
    )
    target_margins: tuple[Decimal, ...] = (
        Decimal("0.15"),
        Decimal("0.25"),
        Decimal("0.35"),
    )

    @field_validator("platform_fee_rate")
    @classmethod
    def validate_platform_fee(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or not Decimal(0) <= value < Decimal(1):
            raise ValueError("Platform fee must be finite and between 0 and 1.")
        return value

    @field_validator("target_margins")
    @classmethod
    def validate_target_margins(
        cls,
        values: tuple[Decimal, ...],
    ) -> tuple[Decimal, ...]:
        if not 2 <= len(values) <= 3:
            raise ValueError("Provide two or three pricing scenarios.")

        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("Target margins must be finite and non-negative.")

        if len(set(values)) != len(values):
            raise ValueError("Target margins must be unique.")

        return values

    @model_validator(mode="after")
    def validate_margin_limits(self) -> "PricingPolicy":
        available_share = Decimal(1) - self.platform_fee_rate

        if any(margin >= available_share for margin in self.target_margins):
            raise ValueError(
                "Target margin must be lower than the revenue share "
                "remaining after platform fees."
            )

        return self

    def evaluate(self, recipe_cost: RecipeCost) -> PricingAnalysis:
        cost = recipe_cost.total_cmv
        available_share = Decimal(1) - self.platform_fee_rate

        break_even_price = (cost / available_share).quantize(
            CENT, rounding=ROUND_CEILING
        )

        scenarios: list[PricingScenario] = []

        for target_margin in self.target_margins:
            denominator = available_share - target_margin

            selling_price = (cost / denominator).quantize(CENT, rounding=ROUND_CEILING)

            while True:
                platform_fee = selling_price * self.platform_fee_rate
                net_revenue = selling_price - platform_fee
                profit = net_revenue - cost
                realized_margin = profit / selling_price

                if realized_margin >= target_margin:
                    break

                selling_price += CENT

            scenarios.append(
                PricingScenario(
                    target_margin_on_gross_price=target_margin,
                    selling_price=selling_price,
                    platform_fee=platform_fee,
                    net_revenue=net_revenue,
                    estimated_profit=profit,
                    realized_margin_on_gross_price=realized_margin,
                )
            )

        return PricingAnalysis(
            platform_fee_rate=self.platform_fee_rate,
            break_even_price=break_even_price,
            scenarios=tuple(scenarios),
        )
