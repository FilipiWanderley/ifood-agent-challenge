from decimal import Decimal

import pytest

from sabor_da_maria_agent.domain.costing import (
    CostingError,
    DeliveryPricingCalculator,
    PricingAnalysis,
    PricingPolicy,
    RecipeCost,
    RecipeCostCalculator,
    RecipeIngredientRequirement,
)
from sabor_da_maria_agent.domain.pantry import IngredientCost, PantryCatalog
from sabor_da_maria_agent.domain.units import MeasureDimension

CENT = Decimal("0.01")
FEE_RATE = Decimal("0.10")
NET_SHARE = Decimal("0.90")
MARGINS = (Decimal("0.15"), Decimal("0.25"), Decimal("0.35"))

# Recipe used across tests: 2000 g at R$ 0.015/g -> total CMV R$ 30.00.
TOTAL_CMV = Decimal("30.00")

# Expected values were obtained independently of the implementation by a
# brute-force search for the smallest cent price meeting each target margin
# (and, for break-even, the smallest cent price with 0.90 * P >= CMV).
EXPECTED_PER_PORTION = {
    1: (Decimal(30), Decimal("33.34"), ("40.00", "46.16", "54.55")),
    4: (Decimal("7.5"), Decimal("8.34"), ("10.00", "11.54", "13.64")),
    15: (Decimal(2), Decimal("2.23"), ("2.67", "3.08", "3.64")),
}


def make_catalog() -> PantryCatalog:
    return PantryCatalog(
        ingredients=(
            IngredientCost(
                name="Peito de frango",
                stock_quantity=Decimal(2),
                stock_unit="kg",
                stock_base_quantity=Decimal(2000),
                purchase_quantity=Decimal(2),
                purchase_unit="kg",
                purchase_base_quantity=Decimal(2000),
                purchase_total=Decimal("30.00"),
                base_unit="g",
                dimension=MeasureDimension.MASS,
                unit_cost=Decimal("0.015"),
            ),
        ),
        historical_purchase_total=Decimal("30.00"),
    )


def make_recipe_cost(
    servings: int,
    quantity: Decimal = Decimal(2000),
) -> RecipeCost:
    return RecipeCostCalculator().calculate(
        recipe_name="Galinhada",
        requirements=[
            RecipeIngredientRequirement(
                name="Peito de frango",
                quantity=quantity,
                unit="g",
            )
        ],
        catalog=make_catalog(),
        servings=servings,
    )


def assert_scenario_math(analysis: PricingAnalysis) -> None:
    assert analysis.platform_fee_rate == FEE_RATE
    assert analysis.break_even_price * NET_SHARE >= analysis.cost_basis
    assert (analysis.break_even_price - CENT) * NET_SHARE < analysis.cost_basis
    assert (
        tuple(scenario.target_margin_on_gross_price for scenario in analysis.scenarios)
        == MARGINS
    )

    for scenario in analysis.scenarios:
        price = scenario.selling_price
        target = scenario.target_margin_on_gross_price

        assert price == price.quantize(CENT)
        assert scenario.platform_fee == price * FEE_RATE
        assert scenario.net_revenue == price * NET_SHARE
        assert scenario.estimated_profit == (scenario.net_revenue - analysis.cost_basis)
        assert scenario.realized_margin_on_gross_price == (
            scenario.estimated_profit / price
        )
        assert scenario.realized_margin_on_gross_price >= target

        # Minimality: one cent less no longer reaches the target margin.
        cheaper = price - CENT
        assert (cheaper * NET_SHARE - analysis.cost_basis) / cheaper < target


@pytest.mark.parametrize("servings", [1, 4, 15])
def test_prices_one_portion_from_cmv_per_portion(servings: int) -> None:
    recipe_cost = make_recipe_cost(servings)
    expected_cost, expected_break_even, expected_prices = EXPECTED_PER_PORTION[servings]

    report = DeliveryPricingCalculator().calculate(recipe_cost)

    assert report.servings == servings
    assert report.total_cmv == TOTAL_CMV
    assert report.cmv_per_portion == expected_cost
    assert report.per_portion.cost_basis == expected_cost
    assert report.per_portion.break_even_price == expected_break_even
    assert (
        tuple(str(scenario.selling_price) for scenario in report.per_portion.scenarios)
        == expected_prices
    )
    assert_scenario_math(report.per_portion)
    assert report.batch is None
    assert report.cmv_complete is True
    assert report.pending_cost_items == ()


@pytest.mark.parametrize("servings", [1, 4, 15])
def test_batch_pricing_is_optional_and_uses_total_cmv(servings: int) -> None:
    recipe_cost = make_recipe_cost(servings)

    report = DeliveryPricingCalculator().calculate(
        recipe_cost,
        include_batch=True,
    )

    assert report.batch is not None
    assert report.batch.cost_basis == TOTAL_CMV
    assert report.batch.break_even_price == Decimal("33.34")
    assert tuple(
        str(scenario.selling_price) for scenario in report.batch.scenarios
    ) == ("40.00", "46.16", "54.55")
    assert_scenario_math(report.batch)


def test_single_serving_portion_and_batch_pricing_are_identical() -> None:
    report = DeliveryPricingCalculator().calculate(
        make_recipe_cost(servings=1),
        include_batch=True,
    )

    assert report.batch == report.per_portion


def test_portion_price_matches_recipe_scaled_down_to_one_serving() -> None:
    batch_recipe = make_recipe_cost(servings=15, quantity=Decimal(1875))
    single_recipe = make_recipe_cost(servings=1, quantity=Decimal(125))

    calculator = DeliveryPricingCalculator()

    assert (
        calculator.calculate(batch_recipe).per_portion
        == calculator.calculate(single_recipe).per_portion
    )


def test_legacy_evaluate_keeps_total_cmv_basis() -> None:
    recipe_cost = make_recipe_cost(servings=15)

    legacy = PricingPolicy().evaluate(recipe_cost)

    assert legacy.cost_basis == recipe_cost.total_cmv
    assert legacy == PricingPolicy().evaluate_cost(recipe_cost.total_cmv)


def test_uses_injected_pricing_policy() -> None:
    policy = PricingPolicy(
        platform_fee_rate=Decimal("0.20"),
        target_margins=(Decimal("0.10"), Decimal("0.30")),
    )

    report = DeliveryPricingCalculator(policy).calculate(make_recipe_cost(servings=4))

    assert report.platform_fee_rate == Decimal("0.20")
    assert report.per_portion.platform_fee_rate == Decimal("0.20")
    assert len(report.per_portion.scenarios) == 2
    # 7.50 / (1 - 0.20) = 9.375 -> 9.38
    assert report.per_portion.break_even_price == Decimal("9.38")


def test_pending_cost_items_mark_cmv_as_partial_without_changing_cost() -> None:
    recipe_cost = make_recipe_cost(servings=15)
    calculator = DeliveryPricingCalculator()

    complete = calculator.calculate(recipe_cost)
    partial = calculator.calculate(
        recipe_cost,
        pending_cost_items=[" Sal ", "Salsinha (cheiro-verde)"],
    )

    assert partial.cmv_complete is False
    assert partial.pending_cost_items == ("Sal", "Salsinha (cheiro-verde)")
    assert partial.total_cmv == complete.total_cmv
    assert partial.per_portion == complete.per_portion


@pytest.mark.parametrize(
    ("pending", "message"),
    [
        (["peito de FRANGO"], "already quantified"),
        (["Sal", "sal"], "Duplicate pending"),
        (["  "], "cannot be empty"),
        ("Sal", "sequence of names"),
    ],
)
def test_rejects_invalid_pending_cost_items(
    pending: list[str] | str,
    message: str,
) -> None:
    with pytest.raises(CostingError, match=message):
        DeliveryPricingCalculator().calculate(
            make_recipe_cost(servings=15),
            pending_cost_items=pending,
        )


@pytest.mark.parametrize(
    "cost",
    [Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")],
)
def test_evaluate_cost_rejects_invalid_cost_basis(cost: Decimal) -> None:
    with pytest.raises(CostingError):
        PricingPolicy().evaluate_cost(cost)
