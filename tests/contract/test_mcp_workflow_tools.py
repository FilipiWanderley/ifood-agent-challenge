"""MCP contract tests for the official planning workflow tools.

Uses an isolated synthetic workbook and a temporary SQLite database; no model
or network calls.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from openpyxl import Workbook

from sabor_da_maria_agent.infrastructure.mcp.server import _load_catalog, mcp
from sabor_da_maria_agent.infrastructure.mcp.workflow_tools import (
    register_workflow_tools,
)
from sabor_da_maria_agent.infrastructure.persistence.sqlite import DB_PATH_ENV

LEGACY_TOOLS = {
    "get_pantry_summary",
    "get_pantry_inventory",
    "calculate_recipe_cost",
    "calculate_delivery_pricing",
}
WORKFLOW_TOOLS = {
    "start_planning_session",
    "update_kitchen_profile",
    "register_recipe_candidate",
    "evaluate_recipe_candidate",
    "approve_recipe_candidate",
    "calculate_approved_recipe",
    "get_planning_status",
}

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
RECIPE: dict[str, Any] = {
    "recipe_id": "galinhada",
    "name": "Galinhada",
    "description": "Arroz com frango.",
    "servings": 4,
    "ingredients": [
        {"name": "Arroz", "quantity": "400", "unit": "g"},
        {"name": "Frango", "quantity": "800", "unit": "g"},
    ],
    "required_equipment": [{"name": "Panela de pressão"}],
    "required_skills": ["Refogar"],
    "required_utilities": ["gas"],
    "storage_requirements": [],
    "minimum_people": 1,
    "total_time_minutes": 60,
}
SOURCES = [
    {
        "title": "Galinhada",
        "url": "https://receitas.example/galinhada",
        "excerpt": "Arroz e frango.",
        "retrieved_at": "2026-11-23T12:00:00-03:00",
    }
]
QUOTES = [
    {
        "name": "Frango",
        "package_quantity": "1",
        "package_unit": "kg",
        "package_price": "25.00",
        "source": "https://mercado.example/frango",
    }
]


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workbook = tmp_path / "pantry.xlsx"
    book = Workbook()
    stock = book.active
    assert stock is not None
    stock.title = "Despensa"
    stock.append(["Ingrediente", "Quantidade em estoque", "Unidade"])
    stock.append(["Arroz", 1, "kg"])
    stock.append(["Frango", 0.5, "kg"])
    prices = book.create_sheet("Precos")
    prices.append(
        ["Ingrediente", "Quantidade comprada", "Unidade", "Preço total pago (R$)"]
    )
    prices.append(["Arroz", 1, "kg", 10])
    prices.append(["Frango", 1, "kg", 30])
    book.save(workbook)
    book.close()
    db_path = tmp_path / "state" / "planning.sqlite3"
    monkeypatch.setenv("SABOR_DA_MARIA_WORKBOOK", str(workbook))
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    return db_path


def call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(mcp) as client:
            response = await client.call_tool(name, arguments or {})
            assert not response.is_error
            assert isinstance(response.data, dict)
            return response.data

    return asyncio.run(run())


def call_fails(name: str, arguments: dict[str, Any]) -> str:
    async def run() -> str:
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as error:
                await client.call_tool(name, arguments)
            return str(error.value)

    return asyncio.run(run())


def ref(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": candidate["session_id"],
        "candidate_id": candidate["candidate_id"],
        "expected_candidate_version": candidate["candidate_version"],
    }


def awaiting_candidate() -> dict[str, Any]:
    session = call("start_planning_session", {"kitchen_profile": KITCHEN})
    candidate = call(
        "register_recipe_candidate",
        {
            "session_id": session["session_id"],
            "recipe": RECIPE,
            "sources": SOURCES,
            "supplemental_quotes": QUOTES,
        },
    )
    assert candidate["status"] == "discovered"
    evaluated = call("evaluate_recipe_candidate", ref(candidate))
    assert evaluated["status"] == "awaiting_approval", evaluated["evaluation"]
    return evaluated


def test_all_eleven_tools_are_registered_with_contracts() -> None:
    async def run() -> dict[str, dict[str, Any]]:
        async with Client(mcp) as client:
            return {tool.name: tool.input_schema for tool in await client.list_tools()}

    schemas = asyncio.run(run())
    assert LEGACY_TOOLS | WORKFLOW_TOOLS <= schemas.keys()
    assert set(schemas["approve_recipe_candidate"]["required"]) == {
        "session_id",
        "candidate_id",
        "expected_candidate_version",
        "decision",
        "user_confirmation",
    }
    assert set(schemas["calculate_approved_recipe"]["required"]) == {
        "session_id",
        "candidate_id",
        "expected_candidate_version",
    }
    assert set(schemas["register_recipe_candidate"]["required"]) == {
        "session_id",
        "recipe",
        "sources",
    }
    assert (
        "production_confirmation" in schemas["register_recipe_candidate"]["properties"]
    )
    assert (
        "production_confirmation"
        not in schemas["register_recipe_candidate"]["required"]
    )
    assert schemas["get_planning_status"]["required"] == ["session_id"]
    assert schemas["start_planning_session"].get("required", []) == []


def test_production_confirmation_is_persisted_and_used_by_mcp(
    isolated_env: Path,
) -> None:
    session = call(
        "start_planning_session",
        {"kitchen_profile": {**KITCHEN, "target_servings_per_day": 8}},
    )
    confirmation = {
        "confirmed_batch_count": 2,
        "production_minutes": 120,
        "user_confirmation": "Confirmo dois lotes em 120 minutos.",
    }
    candidate = call(
        "register_recipe_candidate",
        {
            "session_id": session["session_id"],
            "recipe": RECIPE,
            "sources": SOURCES,
            "supplemental_quotes": QUOTES,
            "production_confirmation": confirmation,
        },
    )
    status = call("get_planning_status", {"session_id": session["session_id"]})
    saved = status["candidates"][0]["production_confirmation"]
    assert saved["confirmed_batch_count"] == 2
    assert saved["production_minutes"] == 120

    evaluated = call("evaluate_recipe_candidate", ref(candidate))
    assert evaluated["status"] == "awaiting_approval", evaluated["evaluation"]
    plan = evaluated["evaluation"]["production_plan"]
    assert plan["batch_count"] == 2
    assert plan["total_produced_servings"] == 8
    chicken = next(
        item for item in evaluated["evaluation"]["pantry"] if item["name"] == "Frango"
    )
    assert Decimal(chicken["required_quantity"]) == Decimal(1600)
    assert Decimal(chicken["shortfall_quantity"]) == Decimal(1100)
    assert Decimal(
        evaluated["evaluation"]["purchase_plan"]["supplemental_purchase_total"]
    ) == Decimal("50.00")


def test_official_workflow_end_to_end(isolated_env: Path) -> None:
    candidate = awaiting_candidate()
    plan = candidate["evaluation"]["purchase_plan"]
    assert Decimal(plan["supplemental_purchase_total"]) == Decimal("25.00")
    assert Decimal(plan["budget_remaining_after_purchase"]) == Decimal("55.00")
    assert candidate["source_evidence"][0]["verification_status"] == (
        "unverified_search_result"
    )

    approved = call(
        "approve_recipe_candidate",
        {**ref(candidate), "decision": "approve", "user_confirmation": "Sim, aprovo."},
    )
    assert approved["status"] == "approved"
    assert approved["approval"]["candidate_version"] == candidate["candidate_version"]

    priced = call("calculate_approved_recipe", ref(approved))
    assert priced["status"] == "priced"
    calc = priced["calculation"]
    assert Decimal(calc["total_cmv_brl"]) == Decimal("26.5")
    assert Decimal(calc["cmv_per_portion_brl"]) == Decimal("6.625")
    assert calc["cmv_complete"] is True

    # The official result must match the compatibility tool for the same inputs.
    legacy = call(
        "calculate_delivery_pricing",
        {
            "recipe_name": RECIPE["name"],
            "requirements": [
                {"name": "Arroz", "quantity": "400", "unit": "g"},
                {"name": "Frango", "quantity": "800", "unit": "g"},
            ],
            "servings": 4,
            "supplemental_quotes": QUOTES,
        },
    )
    assert legacy["delivery_pricing"] == calc["delivery_pricing"]
    assert legacy["total_cmv_brl"] == calc["total_cmv_brl"]

    status = call(
        "get_planning_status",
        {"session_id": priced["session_id"], "candidate_id": priced["candidate_id"]},
    )
    assert status["candidates"][0]["status"] == "priced"
    assert status["events"][-1]["to_status"] == "priced"


def test_calculate_approved_recipe_rejects_unapproved_candidate(
    isolated_env: Path,
) -> None:
    candidate = awaiting_candidate()
    message = call_fails("calculate_approved_recipe", ref(candidate))
    assert "explicit approval" in message
    status = call("get_planning_status", {"session_id": candidate["session_id"]})
    assert status["candidates"][0]["status"] == "awaiting_approval"
    assert status["candidates"][0]["calculation"] is None


def test_kitchen_update_tool_invalidates_and_reports_candidates(
    isolated_env: Path,
) -> None:
    candidate = awaiting_candidate()
    session = call("get_planning_status", {"session_id": candidate["session_id"]})
    updated = call(
        "update_kitchen_profile",
        {
            "session_id": candidate["session_id"],
            "expected_session_version": session["session_version"],
            "kitchen_profile": {"skills": []},
        },
    )
    assert updated["invalidated_candidates"] == [candidate["candidate_id"]]
    message = call_fails(
        "approve_recipe_candidate",
        {**ref(candidate), "decision": "approve", "user_confirmation": "Sim"},
    )
    assert "Stale candidate version" in message


def test_state_survives_new_client_connections(isolated_env: Path) -> None:
    candidate = awaiting_candidate()
    assert isolated_env.exists()
    # Every call above used a new Client connection; read again from disk.
    status = call("get_planning_status", {"session_id": candidate["session_id"]})
    assert (
        status["candidates"][0]["candidate_version"] == candidate["candidate_version"]
    )


def test_database_is_created_lazily(isolated_env: Path) -> None:
    server = FastMCP(name="lazy-test")
    register_workflow_tools(server, _load_catalog)
    assert not isolated_env.exists()

    async def run() -> None:
        async with Client(server) as client:
            await client.call_tool("start_planning_session", {})

    asyncio.run(run())
    assert isolated_env.exists()


@pytest.mark.parametrize(
    ("tool", "arguments", "fragment"),
    [
        ("get_planning_status", {"session_id": "1' OR '1'='1"}, "hex id"),
        ("get_planning_status", {}, "session_id"),
        (
            "evaluate_recipe_candidate",
            {
                "session_id": "a" * 32,
                "candidate_id": "b" * 32,
                "expected_candidate_version": "abc",
            },
            "expected_candidate_version",
        ),
        (
            "approve_recipe_candidate",
            {
                "session_id": "a" * 32,
                "candidate_id": "b" * 32,
                "expected_candidate_version": 1,
                "decision": "maybe",
                "user_confirmation": "?",
            },
            "decision",
        ),
        (
            "calculate_approved_recipe",
            {
                "session_id": "a" * 32,
                "candidate_id": "b" * 32,
                "expected_candidate_version": 1,
            },
            "not found",
        ),
    ],
)
def test_invalid_tool_arguments_are_rejected(
    isolated_env: Path, tool: str, arguments: dict[str, Any], fragment: str
) -> None:
    assert fragment in call_fails(tool, arguments)


def test_register_rejects_float_money_and_unverifiable_quote(
    isolated_env: Path,
) -> None:
    session = call("start_planning_session")
    base = {"session_id": session["session_id"], "recipe": RECIPE, "sources": SOURCES}
    call_fails(
        "register_recipe_candidate",
        {**base, "supplemental_quotes": [{**QUOTES[0], "package_price": 25.0}]},
    )
    call_fails(
        "register_recipe_candidate",
        {**base, "supplemental_quotes": [{**QUOTES[0], "source": "vizinha"}]},
    )
    status = call("get_planning_status", {"session_id": session["session_id"]})
    assert status["candidates"] == []


def test_legacy_tools_still_work(isolated_env: Path) -> None:
    summary = call("get_pantry_summary")
    assert summary["ingredient_count"] == 2
    assert Decimal(summary["remaining_budget_brl"]) == Decimal(80)
    inventory = call("get_pantry_inventory")
    assert {item["name"] for item in inventory["ingredients"]} == {"Arroz", "Frango"}
    cost = call(
        "calculate_recipe_cost",
        {
            "recipe_name": "Arroz",
            "requirements": [{"name": "Arroz", "quantity": "100", "unit": "g"}],
        },
    )
    assert Decimal(cost["total_cmv_brl"]) == Decimal("1.00")
