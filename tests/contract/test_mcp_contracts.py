"""MCP contract tests using an isolated synthetic workbook (no model/API calls)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from openpyxl import Workbook

from sabor_da_maria_agent.infrastructure.mcp.server import mcp

EXPECTED_TOOLS = {
    "get_pantry_summary",
    "get_pantry_inventory",
    "calculate_recipe_cost",
    "calculate_delivery_pricing",
}


@pytest.fixture
def isolated_workbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Make all four tools read this fixture, never Dona Maria's workbook."""
    path = tmp_path / "isolated_pantry.xlsx"
    book = Workbook()
    stock = book.active
    assert stock is not None
    stock.title = "Despensa"
    stock.append(["Ingrediente", "Quantidade em estoque", "Unidade"])
    stock.append(["Peito de frango", 2, "kg"])

    prices = book.create_sheet("Precos")
    prices.append(
        [
            "Ingrediente",
            "Quantidade comprada",
            "Unidade",
            "Preço total pago (R$)",
        ]
    )
    prices.append(["Peito de frango", 2, "kg", 30])
    book.save(path)
    book.close()
    monkeypatch.setenv("SABOR_DA_MARIA_WORKBOOK", str(path))
    return path


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(mcp) as client:
            response = await client.call_tool(name, arguments or {})
            assert not response.is_error
            assert isinstance(response.data, dict)
            return response.data

    return asyncio.run(run())


def call_tool_must_fail(name: str, arguments: dict[str, Any]) -> str:
    async def run() -> str:
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as error:
                await client.call_tool(name, arguments)
            return str(error.value)

    return asyncio.run(run())


def recipe_args(
    *,
    quantity: str = "2000",
    servings: int = 4,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "recipe_name": "Galinhada de teste",
        "requirements": [
            {"name": "Peito de frango", "quantity": quantity, "unit": "g"}
        ],
        "servings": servings,
        **extra,
    }


def quote(price: str) -> dict[str, str]:
    return {
        "name": "Peito de frango",
        "package_quantity": "1",
        "package_unit": "kg",
        "package_price": price,
        "source": "https://example.org/test-quote",
    }


def test_tools_are_discoverable_with_expected_input_contracts() -> None:
    async def run() -> dict[str, dict[str, Any]]:
        async with Client(mcp) as client:
            tools = await client.list_tools()
            return {tool.name: tool.input_schema for tool in tools}

    schemas = asyncio.run(run())
    assert EXPECTED_TOOLS <= schemas.keys()

    for name in ("get_pantry_summary", "get_pantry_inventory"):
        assert schemas[name].get("required", []) == []

    for name in ("calculate_recipe_cost", "calculate_delivery_pricing"):
        schema = schemas[name]
        assert {"recipe_name", "requirements"} <= set(schema["required"])
        assert schema["properties"]["requirements"]["type"] == "array"
        assert schema["properties"]["servings"]["type"] == "integer"


def test_summary_matches_synthetic_purchase_and_budget(
    isolated_workbook: Path,
) -> None:
    data = call_tool("get_pantry_summary")
    assert data["ingredient_count"] == 1
    assert data["workbook"] == isolated_workbook.name
    assert Decimal(data["historical_purchase_total_brl"]) == Decimal(30)
    assert Decimal(data["remaining_budget_brl"]) == Decimal(80)


def test_inventory_reconciles_with_summary_and_units(
    isolated_workbook: Path,
) -> None:
    summary = call_tool("get_pantry_summary")
    data = call_tool("get_pantry_inventory")
    assert data["ingredient_count"] == summary["ingredient_count"]
    assert data["remaining_budget_brl"] == summary["remaining_budget_brl"]
    assert (
        data["historical_purchase_total_brl"]
        == summary["historical_purchase_total_brl"]
    )
    assert len(data["ingredients"]) == 1
    chicken = data["ingredients"][0]
    assert chicken["name"] == "Peito de frango"
    assert Decimal(chicken["stock_base_quantity"]) == Decimal(2000)
    assert chicken["base_unit"] == "g"
    assert Decimal(chicken["purchase_total_brl"]) == Decimal(30)
    assert Decimal(chicken["unit_cost_brl"]) == Decimal("0.015")


def test_cost_tool_reconciles_stock_and_servings(isolated_workbook: Path) -> None:
    result = call_tool(
        "calculate_recipe_cost",
        recipe_args(quantity="1000", servings=4),
    )
    assert Decimal(result["total_cmv_brl"]) == Decimal(15)
    assert Decimal(result["cmv_per_portion_brl"]) == Decimal("3.75")
    assert Decimal(result["supplemental_purchase_total_brl"]) == Decimal(0)
    assert Decimal(result["budget_remaining_after_purchase_brl"]) == Decimal(80)
    assert result["within_budget"] is True


@pytest.mark.parametrize(
    "requirements",
    [
        [{"name": "Peito de frango", "unit": "g"}],  # missing quantity
        [{"name": "Peito de frango", "quantity": "100", "unit": "g", "extra": "x"}],
        [],
    ],
)
def test_cost_rejects_incomplete_or_unexpected_requirements(
    isolated_workbook: Path,
    requirements: list[dict[str, str]],
) -> None:
    error = call_tool_must_fail(
        "calculate_recipe_cost",
        {"recipe_name": "Teste", "requirements": requirements},
    )
    assert error


@pytest.mark.parametrize("quantity", ["not-a-number", "1,5"])
def test_cost_rejects_invalid_decimal_input(
    isolated_workbook: Path,
    quantity: str,
) -> None:
    error = call_tool_must_fail(
        "calculate_recipe_cost",
        recipe_args(quantity=quantity),
    )
    assert error


def test_cost_requires_a_real_quote_for_missing_stock(
    isolated_workbook: Path,
) -> None:
    error = call_tool_must_fail(
        "calculate_recipe_cost",
        recipe_args(quantity="3000"),
    )
    assert error


def test_supplemental_purchase_respects_budget(
    isolated_workbook: Path,
) -> None:
    result = call_tool(
        "calculate_recipe_cost",
        recipe_args(quantity="3000", supplemental_quotes=[quote("18.00")]),
    )
    assert Decimal(result["supplemental_purchase_total_brl"]) == Decimal(18)
    assert Decimal(result["budget_remaining_after_purchase_brl"]) == Decimal(62)
    assert Decimal(result["total_cmv_brl"]) == Decimal(48)
    assert result["within_budget"] is True


def test_supplemental_purchase_reports_budget_overrun(
    isolated_workbook: Path,
) -> None:
    result = call_tool(
        "calculate_recipe_cost",
        recipe_args(quantity="3000", supplemental_quotes=[quote("100.00")]),
    )
    assert Decimal(result["supplemental_purchase_total_brl"]) == Decimal(100)
    assert result["within_budget"] is False
    assert Decimal(result["budget_remaining_after_purchase_brl"]) == Decimal(-20)


def test_quote_missing_source_is_rejected(isolated_workbook: Path) -> None:
    invalid_quote = quote("18.00")
    invalid_quote.pop("source")
    error = call_tool_must_fail(
        "calculate_recipe_cost",
        recipe_args(quantity="3000", supplemental_quotes=[invalid_quote]),
    )
    assert error


def test_pricing_agrees_with_cost_tool_and_ten_percent_fee(
    isolated_workbook: Path,
) -> None:
    arguments = recipe_args()
    cost = call_tool("calculate_recipe_cost", arguments)
    price = call_tool("calculate_delivery_pricing", arguments)
    assert Decimal(cost["total_cmv_brl"]) == Decimal(price["total_cmv_brl"])
    assert Decimal(cost["cmv_per_portion_brl"]) == Decimal(price["cmv_per_portion_brl"])
    assert Decimal(price["platform_fee_rate"]) == Decimal("0.10")
    assert price["currency"] == "BRL"
    assert price["servings"] == 4
    assert price["break_even_price_per_portion_brl"] == "8.34"
    assert price["delivery_pricing"]["batch"] is None
    assert price["cmv_complete"] is True

    per_portion = price["delivery_pricing"]["per_portion"]
    assert Decimal(per_portion["cost_basis"]) == Decimal("7.5")
    scenarios = per_portion["scenarios"]
    assert [item["target_margin_on_gross_price"] for item in scenarios] == [
        "0.15",
        "0.25",
        "0.35",
    ]
    for item in scenarios:
        selling_price = Decimal(item["selling_price"])
        net = Decimal(item["net_revenue"])
        profit = Decimal(item["estimated_profit"])
        assert Decimal(item["platform_fee"]) == selling_price * Decimal("0.10")
        assert net == selling_price * Decimal("0.90")
        assert profit == net - Decimal("7.5")


def test_pricing_marks_unquantified_ingredients_as_incomplete(
    isolated_workbook: Path,
) -> None:
    result = call_tool(
        "calculate_delivery_pricing",
        recipe_args(pending_cost_items=["Sal", "Cheiro-verde"]),
    )
    assert result["cmv_complete"] is False
    assert result["pending_cost_items"] == ["Sal", "Cheiro-verde"]
    assert Decimal(result["total_cmv_brl"]) == Decimal(30)


def test_pricing_rejects_duplicate_pending_item(
    isolated_workbook: Path,
) -> None:
    error = call_tool_must_fail(
        "calculate_delivery_pricing",
        recipe_args(pending_cost_items=["Peito de frango"]),
    )
    assert error


def test_pricing_can_explicitly_include_batch_scenarios(
    isolated_workbook: Path,
) -> None:
    result = call_tool(
        "calculate_delivery_pricing",
        recipe_args(include_batch_pricing=True),
    )
    batch = result["delivery_pricing"]["batch"]
    assert batch is not None
    assert Decimal(batch["cost_basis"]) == Decimal(30)
    assert Decimal(result["delivery_pricing"]["per_portion"]["cost_basis"]) == (
        Decimal("7.5")
    )


def test_financial_queries_do_not_mutate_the_pantry(
    isolated_workbook: Path,
) -> None:
    initial = call_tool("get_pantry_inventory")
    call_tool("calculate_recipe_cost", recipe_args(quantity="500"))
    call_tool("calculate_delivery_pricing", recipe_args(quantity="500"))
    final = call_tool("get_pantry_inventory")
    assert final == initial
