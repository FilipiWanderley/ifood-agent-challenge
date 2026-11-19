from decimal import Decimal

import pytest
from pydantic import ValidationError

from sabor_da_maria_agent.domain.costing import (
    IncompatibleMeasurementError,
    MissingSupplementalQuoteError,
    PricingPolicy,
    RecipeCostCalculator,
    RecipeIngredientRequirement,
    SupplementalPurchaseQuote,
)
from sabor_da_maria_agent.domain.pantry import IngredientCost, PantryCatalog
from sabor_da_maria_agent.domain.units import MeasureDimension


def make_ingredient(
    name: str,
    stock_quantity: Decimal,
    unit_cost: Decimal,
    base_unit: str = "g",
    dimension: MeasureDimension = MeasureDimension.MASS,
) -> IngredientCost:
    purchase_quantity = Decimal(1000)

    return IngredientCost(
        name=name,
        stock_quantity=stock_quantity,
        stock_unit=base_unit,
        stock_base_quantity=stock_quantity,
        purchase_quantity=purchase_quantity,
        purchase_unit=base_unit,
        purchase_base_quantity=purchase_quantity,
        purchase_total=unit_cost * purchase_quantity,
        base_unit=base_unit,
        dimension=dimension,
        unit_cost=unit_cost,
    )


def make_catalog(
    *ingredients: IngredientCost,
    budget: Decimal = Decimal("80.00"),
) -> PantryCatalog:
    return PantryCatalog(
        ingredients=ingredients,
        historical_purchase_total=Decimal("50.00"),
        remaining_budget=budget,
    )


def make_quote(
    name: str = "Farinha",
    package_price: Decimal = Decimal("20.00"),
) -> SupplementalPurchaseQuote:
    return SupplementalPurchaseQuote(
        name=name,
        package_quantity=Decimal(1),
        package_unit="un 500g",
        package_price=package_price,
        source="https://supplier.example/products/flour",
    )


def test_calculates_recipe_cost_from_existing_stock() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Arroz",
            Decimal(1000),
            Decimal("0.04"),
        )
    )

    result = RecipeCostCalculator().calculate(
        recipe_name="Arroz especial",
        requirements=[
            RecipeIngredientRequirement(
                name="Arroz",
                quantity=Decimal(250),
                unit="g",
            )
        ],
        catalog=catalog,
        servings=5,
    )

    assert result.total_cmv == Decimal("10.00")
    assert result.cmv_per_portion == Decimal("2.00")
    assert result.supplemental_purchase_total == Decimal(0)
    assert result.lines[0].remaining_stock_quantity == Decimal(750)
    assert result.within_budget is True


def test_calculates_supplemental_purchase_and_consumed_cost() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Farinha",
            Decimal(100),
            Decimal("0.05"),
        )
    )

    result = RecipeCostCalculator().calculate(
        recipe_name="Bolo caseiro",
        requirements=[
            RecipeIngredientRequirement(
                name="Farinha",
                quantity=Decimal(200),
                unit="g",
            )
        ],
        catalog=catalog,
        supplemental_quotes=[make_quote()],
        servings=4,
    )

    line = result.lines[0]

    assert line.stock_used_quantity == Decimal(100)
    assert line.supplemental_quantity == Decimal(100)
    assert line.supplemental_unit_cost == Decimal("0.04")
    assert line.consumed_cost == Decimal("9.00")
    assert line.purchase_package_count == 1
    assert line.purchase_base_quantity == Decimal(500)
    assert line.purchase_total == Decimal("20.00")
    assert line.remaining_stock_quantity == Decimal(400)
    assert result.total_cmv == Decimal("9.00")
    assert result.supplemental_purchase_total == Decimal("20.00")
    assert result.budget_remaining_after_purchase == Decimal("60.00")


def test_calculates_cost_for_ingredient_not_in_catalog() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Arroz",
            Decimal(1000),
            Decimal("0.04"),
        )
    )

    result = RecipeCostCalculator().calculate(
        recipe_name="Bolo",
        requirements=[
            RecipeIngredientRequirement(
                name="Farinha",
                quantity=Decimal(200),
                unit="g",
            )
        ],
        catalog=catalog,
        supplemental_quotes=[make_quote()],
    )

    assert result.total_cmv == Decimal("8.00")
    assert result.lines[0].stock_available_quantity == Decimal(0)
    assert result.lines[0].remaining_stock_quantity == Decimal(300)
    assert result.supplemental_purchase_total == Decimal("20.00")


def test_requires_quote_when_stock_is_insufficient() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Farinha",
            Decimal(100),
            Decimal("0.05"),
        )
    )

    with pytest.raises(MissingSupplementalQuoteError):
        RecipeCostCalculator().calculate(
            recipe_name="Bolo",
            requirements=[
                RecipeIngredientRequirement(
                    name="Farinha",
                    quantity=Decimal(200),
                    unit="g",
                )
            ],
            catalog=catalog,
        )


def test_rejects_incompatible_measurement_dimensions() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Leite",
            Decimal(1000),
            Decimal("0.01"),
            base_unit="ml",
            dimension=MeasureDimension.VOLUME,
        )
    )

    with pytest.raises(IncompatibleMeasurementError):
        RecipeCostCalculator().calculate(
            recipe_name="Receita",
            requirements=[
                RecipeIngredientRequirement(
                    name="Leite",
                    quantity=Decimal(100),
                    unit="g",
                )
            ],
            catalog=catalog,
        )


def test_aggregates_repeated_ingredients_before_buying_packages() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Farinha",
            Decimal(100),
            Decimal("0.05"),
        )
    )

    result = RecipeCostCalculator().calculate(
        recipe_name="Receita",
        requirements=[
            RecipeIngredientRequirement(
                name="Farinha",
                quantity=Decimal(100),
                unit="g",
            ),
            RecipeIngredientRequirement(
                name="Farinha",
                quantity=Decimal(100),
                unit="g",
            ),
        ],
        catalog=catalog,
        supplemental_quotes=[make_quote()],
    )

    assert len(result.lines) == 1
    assert result.lines[0].required_quantity == Decimal(200)
    assert result.lines[0].purchase_package_count == 1
    assert result.total_cmv == Decimal("9.00")
    assert result.supplemental_purchase_total == Decimal("20.00")


def test_reports_budget_overrun_without_losing_cost_breakdown() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Farinha",
            Decimal(100),
            Decimal("0.05"),
        ),
        budget=Decimal("10.00"),
    )

    result = RecipeCostCalculator().calculate(
        recipe_name="Bolo",
        requirements=[
            RecipeIngredientRequirement(
                name="Farinha",
                quantity=Decimal(200),
                unit="g",
            )
        ],
        catalog=catalog,
        supplemental_quotes=[make_quote()],
    )

    assert result.within_budget is False
    assert result.budget_remaining_after_purchase == Decimal("-10.00")
    assert result.total_cmv == Decimal("9.00")


def test_generates_break_even_and_three_pricing_scenarios() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Farinha",
            Decimal(100),
            Decimal("0.05"),
        )
    )

    recipe_cost = RecipeCostCalculator().calculate(
        recipe_name="Bolo",
        requirements=[
            RecipeIngredientRequirement(
                name="Farinha",
                quantity=Decimal(200),
                unit="g",
            )
        ],
        catalog=catalog,
        supplemental_quotes=[make_quote()],
    )

    analysis = PricingPolicy().evaluate(recipe_cost)

    assert analysis.platform_fee_rate == Decimal("0.10")
    assert analysis.break_even_price == Decimal("10.00")
    assert len(analysis.scenarios) == 3
    assert analysis.scenarios[0].selling_price == Decimal("12.00")
    assert analysis.scenarios[1].selling_price == Decimal("13.85")
    assert analysis.scenarios[2].selling_price == Decimal("16.37")

    assert analysis.scenarios[1].platform_fee == Decimal("1.385")
    assert analysis.scenarios[1].net_revenue == Decimal("12.465")
    assert analysis.scenarios[1].estimated_profit == Decimal("3.465")

    for scenario in analysis.scenarios:
        assert scenario.platform_fee == (
            scenario.selling_price * Decimal("0.10")
        )

        assert scenario.net_revenue == (
            scenario.selling_price * Decimal("0.90")
        )

        assert scenario.estimated_profit == (
            scenario.net_revenue - recipe_cost.total_cmv
        )

        assert (
            scenario.realized_margin_on_gross_price
            >= scenario.target_margin_on_gross_price
        )


def test_rejects_invalid_pricing_scenarios() -> None:
    with pytest.raises(ValidationError):
        PricingPolicy(target_margins=(Decimal("0.15"),))

    with pytest.raises(ValidationError):
        PricingPolicy(
            target_margins=(
                Decimal("0.15"),
                Decimal("0.90"),
            )
        )


def test_rejects_non_integer_servings() -> None:
    catalog = make_catalog(
        make_ingredient(
            "Arroz",
            Decimal(1000),
            Decimal("0.04"),
        )
    )

    with pytest.raises(ValueError):
        RecipeCostCalculator().calculate(
            recipe_name="Arroz",
            requirements=[
                RecipeIngredientRequirement(
                    name="Arroz",
                    quantity=Decimal(100),
                    unit="g",
                )
            ],
            catalog=catalog,
            servings=1.5,
        )
