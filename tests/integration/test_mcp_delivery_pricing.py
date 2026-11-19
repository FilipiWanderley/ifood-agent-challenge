import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from openpyxl import Workbook

from sabor_da_maria_agent.infrastructure.mcp.server import mcp

MARGINS = ("0.15", "0.25", "0.35")
CENT = Decimal("0.01")


def create_workbook(path: Path) -> None:
    """Workbook with one ingredient: 2 kg of chicken bought for R$ 30.00."""
    workbook = Workbook()

    stock_sheet = workbook.active
    assert stock_sheet is not None
    stock_sheet.title = "Despensa"
    stock_sheet.append(["Ingrediente", "Quantidade em estoque", "Unidade"])
    stock_sheet.append(["Peito de frango", 2, "kg"])

    purchase_sheet = workbook.create_sheet("Precos")
    purchase_sheet.append(
        [
            "Ingrediente",
            "Quantidade comprada",
            "Unidade",
            "Preço total pago (R$)",
        ]
    )
    purchase_sheet.append(["Peito de frango", 2, "kg", 30])

    workbook.save(path)
    workbook.close()


@pytest.fixture
def synthetic_workbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    path = tmp_path / "despensa_teste.xlsx"
    create_workbook(path)
    monkeypatch.setenv("SABOR_DA_MARIA_WORKBOOK", str(path))
    return path


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments)
            assert not result.is_error
            data: dict[str, Any] = result.data
            return data

    return asyncio.run(run())


def chicken_arguments(servings: int, **extra: Any) -> dict[str, Any]:
    return {
        "recipe_name": "Galinhada",
        "requirements": [{"name": "Peito de frango", "quantity": "2000", "unit": "g"}],
        "servings": servings,
        **extra,
    }


def assert_scenarios(analysis: dict[str, Any]) -> None:
    cost = Decimal(analysis["cost_basis"])
    scenarios = analysis["scenarios"]

    assert [s["target_margin_on_gross_price"] for s in scenarios] == list(MARGINS)

    for scenario in scenarios:
        price = Decimal(scenario["selling_price"])
        target = Decimal(scenario["target_margin_on_gross_price"])
        net_revenue = Decimal(scenario["net_revenue"])
        profit = Decimal(scenario["estimated_profit"])

        assert Decimal(scenario["platform_fee"]) == price * Decimal("0.10")
        assert net_revenue == price * Decimal("0.90")
        assert profit == net_revenue - cost
        assert profit / price >= target
        cheaper = price - CENT
        assert (cheaper * Decimal("0.90") - cost) / cheaper < target


@pytest.mark.parametrize(
    ("servings", "cmv_per_portion", "break_even", "prices"),
    [
        (1, "30", "33.34", ["40.00", "46.16", "54.55"]),
        (4, "7.5", "8.34", ["10.00", "11.54", "13.64"]),
        (15, "2", "2.23", ["2.67", "3.08", "3.64"]),
    ],
)
def test_tool_returns_explicit_per_portion_pricing(
    synthetic_workbook: Path,
    servings: int,
    cmv_per_portion: str,
    break_even: str,
    prices: list[str],
) -> None:
    data = call_tool("calculate_delivery_pricing", chicken_arguments(servings))

    per_portion = data["delivery_pricing"]["per_portion"]

    assert data["servings"] == servings
    assert Decimal(data["total_cmv_brl"]) == Decimal(30)
    assert Decimal(data["cmv_per_portion_brl"]) == Decimal(cmv_per_portion)
    assert data["break_even_price_per_portion_brl"] == break_even
    assert Decimal(per_portion["cost_basis"]) == Decimal(cmv_per_portion)
    assert per_portion["break_even_price"] == break_even
    assert [s["selling_price"] for s in per_portion["scenarios"]] == prices
    assert_scenarios(per_portion)
    assert data["delivery_pricing"]["batch"] is None
    assert data["cmv_complete"] is True
    assert data["pending_cost_items"] == []


@pytest.mark.parametrize("servings", [1, 4, 15])
def test_tool_includes_batch_pricing_on_request(
    synthetic_workbook: Path,
    servings: int,
) -> None:
    data = call_tool(
        "calculate_delivery_pricing",
        chicken_arguments(servings, include_batch_pricing=True),
    )

    batch = data["delivery_pricing"]["batch"]

    assert Decimal(batch["cost_basis"]) == Decimal(30)
    assert batch["break_even_price"] == "33.34"
    assert [s["selling_price"] for s in batch["scenarios"]] == [
        "40.00",
        "46.16",
        "54.55",
    ]
    assert_scenarios(batch)
    assert data["pricing_analysis"] == batch


@pytest.mark.parametrize("servings", [1, 4, 15])
def test_legacy_fields_keep_total_cmv_basis(
    synthetic_workbook: Path,
    servings: int,
) -> None:
    data = call_tool("calculate_delivery_pricing", chicken_arguments(servings))

    assert data["platform_fee_rate"] == "0.10"
    assert data["currency"] == "BRL"
    assert data["break_even_price_brl"] == "33.34"
    assert Decimal(data["pricing_analysis"]["cost_basis"]) == Decimal(30)
    assert len(data["pricing_analysis"]["scenarios"]) == 3
    assert data["recipe_cost"]["servings"] == servings


def test_tool_flags_pending_costs_without_adding_them_to_cmv(
    synthetic_workbook: Path,
) -> None:
    data = call_tool(
        "calculate_delivery_pricing",
        chicken_arguments(15, pending_cost_items=["Sal", "Cheiro-verde"]),
    )

    assert data["cmv_complete"] is False
    assert data["pending_cost_items"] == ["Sal", "Cheiro-verde"]
    assert data["delivery_pricing"]["cmv_complete"] is False
    assert Decimal(data["total_cmv_brl"]) == Decimal(30)


def test_tool_rejects_pending_item_already_in_requirements(
    synthetic_workbook: Path,
) -> None:
    async def run() -> None:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="already quantified"):
                await client.call_tool(
                    "calculate_delivery_pricing",
                    chicken_arguments(
                        15,
                        pending_cost_items=["Peito de frango"],
                    ),
                )

    asyncio.run(run())


def test_real_workbook_portion_price_matches_one_serving_recipe() -> None:
    """15-serving galinhada vs. the same recipe divided by 15 (real pantry)."""
    batch_requirements = [
        ("Peito de frango", "1875", "g"),
        ("Óleo de soja", "112.5", "ml"),
        ("Cebola", "262.5", "g"),
        ("Alho", "33", "g"),
        ("Tomate", "375", "g"),
        ("Arroz branco tipo 1", "693.75", "g"),
        ("Açafrão em pó (cúrcuma)", "11.25", "g"),
    ]

    def arguments(servings: int, divisor: Decimal) -> dict[str, Any]:
        return {
            "recipe_name": "Galinhada mineira com açafrão",
            "requirements": [
                {
                    "name": name,
                    "quantity": str(Decimal(quantity) / divisor),
                    "unit": unit,
                }
                for name, quantity, unit in batch_requirements
            ],
            "servings": servings,
        }

    batch = call_tool("calculate_delivery_pricing", arguments(15, Decimal(1)))
    single = call_tool("calculate_delivery_pricing", arguments(1, Decimal(15)))

    assert (
        batch["delivery_pricing"]["per_portion"]
        == single["delivery_pricing"]["per_portion"]
    )
    assert_scenarios(batch["delivery_pricing"]["per_portion"])
