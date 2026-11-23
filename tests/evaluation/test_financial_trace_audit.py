"""Pure offline tests for the real-trace financial MCP replay auditor."""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

AUDITOR_PATH = (
    Path(__file__).resolve().parents[2] / "evals" / "audit_financial_trace.py"
)
spec = importlib.util.spec_from_file_location("audit_financial_trace", AUDITOR_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def sample_events() -> list[dict[str, Any]]:
    ingredients = [
        {"name": f"Ingredient {i}", "quantity": str(i), "unit": "g"}
        for i in range(1, 8)
    ]
    cost_input = {
        "recipe_name": "Galinhada",
        "requirements": ingredients,
        "servings": 15,
    }
    pricing_input = {
        **cost_input,
        "pending_cost_items": ["Sal", "Cheiro-verde"],
        "include_batch_pricing": True,
    }
    return [
        {
            "type": "tool_use",
            "name": mod.COST_TOOL,
            "tool_call_id": "c1",
            "input": cost_input,
        },
        {
            "type": "tool_result",
            "name": mod.COST_TOOL,
            "tool_call_id": "c1",
            "output": "clipped",
            "is_error": False,
        },
        {
            "type": "tool_use",
            "name": mod.PRICING_TOOL,
            "tool_call_id": "c2",
            "input": pricing_input,
        },
        {
            "type": "tool_result",
            "name": mod.PRICING_TOOL,
            "tool_call_id": "c2",
            "output": "clipped",
            "is_error": False,
        },
        {
            "type": "result",
            "exit_code": 0,
            "text": "INCOMPLETO. Lote R$ 35,59, porcao R$ 2,37, piso R$ 2,64; opcoes R$ 3,17, R$ 3,66, R$ 4,32",
        },
    ]


def sample_outputs() -> tuple[dict[str, Any], dict[str, Any]]:
    total = Decimal("35.59300")
    per = total / Decimal(15)
    cost = {
        "total_cmv_brl": str(total),
        "cmv_per_portion_brl": str(per),
        "supplemental_purchase_total_brl": "0",
        "budget_remaining_after_purchase_brl": "80",
        "within_budget": True,
    }
    scenarios = []
    for margin, price in zip(mod.EXPECTED_MARGINS, mod.EXPECTED_PRICES, strict=True):
        net = price * Decimal("0.90")
        scenarios.append(
            {
                "target_margin_on_gross_price": str(margin),
                "selling_price": str(price),
                "platform_fee": str(price * Decimal("0.10")),
                "net_revenue": str(net),
                "estimated_profit": str(net - per),
            }
        )
    pricing = {
        "total_cmv_brl": str(total),
        "cmv_per_portion_brl": str(per),
        "cmv_complete": False,
        "pending_cost_items": ["Sal", "Cheiro-verde"],
        "servings": 15,
        "platform_fee_rate": "0.10",
        "currency": "BRL",
        "break_even_price_per_portion_brl": "2.64",
        "delivery_pricing": {
            "batch": {"cost_basis": str(total)},
            "per_portion": {"scenarios": scenarios},
        },
    }
    return cost, pricing


def test_replay_audit_accepts_valid_financial_outputs() -> None:
    cost, pricing = sample_outputs()
    report = mod.audit(sample_events(), cost, pricing)
    assert report["passed"] is True
    assert report["scenarios_brl"] == ["3.17", "3.66", "4.32"]


def test_replay_audit_detects_bad_margin() -> None:
    cost, pricing = sample_outputs()
    pricing["delivery_pricing"]["per_portion"]["scenarios"][0][
        "target_margin_on_gross_price"
    ] = "0.50"
    assert mod.audit(sample_events(), cost, pricing)["passed"] is False


def test_replay_audit_detects_wrong_cost() -> None:
    cost, pricing = sample_outputs()
    pricing["total_cmv_brl"] = "0.01"
    assert mod.audit(sample_events(), cost, pricing)["passed"] is False


def test_replay_audit_detects_missing_incomplete_cost_warning() -> None:
    events = sample_events()
    events[-1]["text"] = (
        "Os precos estao finais! R$ 35,59 R$ 2,37 R$ 2,64 R$ 3,17 R$ 3,66 R$ 4,32"
    )
    cost, pricing = sample_outputs()
    assert mod.audit(events, cost, pricing)["passed"] is False


def test_replay_audit_detects_original_mcp_failure() -> None:
    events = sample_events()
    events[3]["is_error"] = True
    with pytest.raises(ValueError, match="Original Hermes tool call failed"):
        mod.extract_call(events, mod.PRICING_TOOL)


def test_trace_reader_accepts_utf16_powershell(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in sample_events()), encoding="utf-16"
    )
    assert len(mod.load_events(path)) == 5


def test_trace_reader_accepts_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in sample_events()), encoding="utf-8-sig"
    )
    assert len(mod.load_events(path)) == 5


def test_trace_reader_rejects_broken_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Bad JSONL"):
        mod.load_events(path)


def test_repairs_corrupted_windows_cp850_ingredient_names_without_mutating_trace() -> (
    None
):
    events = sample_events()
    corrupted = {
        "name": "├ôleo de soja",
        "quantity": "100",
        "unit": "ml",
    }
    events[0]["input"]["requirements"][0] = corrupted
    original = json.dumps(events, ensure_ascii=False)
    parsed = mod.extract_call(events, mod.COST_TOOL)
    assert parsed["requirements"][0]["name"] == "Óleo de soja"
    assert json.dumps(events, ensure_ascii=False) == original


def test_repairs_multiple_accents_and_preserves_plain_text() -> None:
    value = {
        "recipe_name": "Galinhada",
        "requirements": [
            {"name": "A├ºafr├úo em p├│ (c├║rcuma)", "quantity": "10", "unit": "g"},
            {"name": "Cebola", "quantity": "250", "unit": "g"},
        ],
        "pending_cost_items": ["Sal", "Salsinha (cheiro-verde)"],
    }
    result = mod.repair_windows_mojibake(value)
    assert result["requirements"][0]["name"] == "Açafrão em pó (cúrcuma)"
    assert result["requirements"][1]["name"] == "Cebola"
    assert result["pending_cost_items"] == value["pending_cost_items"]
    assert value["requirements"][0]["name"] != result["requirements"][0]["name"]


def test_keeps_normal_unicode_names_unchanged() -> None:
    names = ["Óleo de soja", "Açafrão em pó (cúrcuma)", "Cebola"]
    assert mod.repair_windows_mojibake(names) == names
