"""Replay the two approved-recipe MCP calls and audit the financial result.

The Hermes JSONL may truncate a tool_result; tool_use inputs are retained.
This verifier makes fresh read-only MCP calls against the *current* workbook.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

COST_TOOL = "mcp__sabor_da_maria__calculate_recipe_cost"
PRICING_TOOL = "mcp__sabor_da_maria__calculate_delivery_pricing"
EXPECTED_TOTAL = Decimal("35.59300")
EXPECTED_PRICES = (Decimal("3.17"), Decimal("3.66"), Decimal("4.32"))
EXPECTED_MARGINS = (Decimal("0.15"), Decimal("0.25"), Decimal("0.35"))
CENT = Decimal("0.01")


def load_events(path: Path) -> list[dict[str, Any]]:
    payload = path.read_bytes()
    encoding = (
        "utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    )
    events: list[dict[str, Any]] = []
    for line_number, raw in enumerate(payload.decode(encoding).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("Session:", "session_id:")):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bad JSONL line {line_number}: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise TypeError(f"Expected JSON object at line {line_number}")
        events.append(event)
    return events


def repair_windows_mojibake(value: Any) -> Any:
    """Repair cp850-decoded UTF-8 in captured Hermes argument *values*.

    PowerShell/Windows console capture can turn 'Óleo' into '├ôleo'.
    Normal UTF-8 strings and original trace events remain unchanged.
    """
    if isinstance(value, str):
        if "├" not in value and "┬" not in value:
            return value
        try:
            restored = value.encode("cp850").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
        return restored if len(restored) < len(value) else value
    if isinstance(value, list):
        return [repair_windows_mojibake(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_windows_mojibake(item) for key, item in value.items()}
    return value


def extract_call(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    calls = [e for e in events if e.get("type") == "tool_use" and e.get("name") == name]
    if len(calls) != 1:
        raise ValueError(f"Expected one {name} call, got {len(calls)}")
    call = calls[0]
    if not isinstance(call.get("input"), dict):
        raise TypeError(f"Missing structured input for {name}")
    matching_results = [
        e
        for e in events
        if e.get("type") == "tool_result"
        and e.get("tool_call_id") == call.get("tool_call_id")
    ]
    if not matching_results or matching_results[-1].get("is_error") is True:
        raise ValueError(f"Original Hermes tool call failed or has no result: {name}")
    return repair_windows_mojibake(call["input"])


async def replay_tools(
    cost_args: dict[str, Any], pricing_args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    from fastmcp import Client

    from sabor_da_maria_agent.infrastructure.mcp.server import mcp

    async with Client(mcp) as client:
        cost_result = await client.call_tool("calculate_recipe_cost", cost_args)
        pricing_result = await client.call_tool(
            "calculate_delivery_pricing", pricing_args
        )
    if cost_result.is_error or pricing_result.is_error:
        raise RuntimeError("MCP replay returned a tool error")
    if not isinstance(cost_result.data, dict) or not isinstance(
        pricing_result.data, dict
    ):
        raise TypeError("MCP replay did not return JSON objects")
    return cost_result.data, pricing_result.data


def money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f").replace(".", ",")


def numeric(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid numeric result: {value!r}") from exc


def audit(
    events: list[dict[str, Any]],
    cost: dict[str, Any],
    pricing: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []

    def check(condition: bool, reason: str) -> None:
        if not condition:
            errors.append(reason)

    cost_args = extract_call(events, COST_TOOL)
    pricing_args = extract_call(events, PRICING_TOOL)
    check(
        cost_args.get("servings") == pricing_args.get("servings") == 15,
        "Expected 15 servings",
    )
    check(
        cost_args.get("requirements") == pricing_args.get("requirements"),
        "Cost and pricing inputs differ",
    )
    check(
        len(cost_args.get("requirements", [])) == 7,
        "Expected seven quantified ingredients",
    )
    check(
        len(pricing_args.get("pending_cost_items", [])) == 2,
        "Expected two pending ingredients",
    )
    check(
        bool(pricing_args.get("include_batch_pricing")),
        "Batch pricing should be requested",
    )

    total = numeric(cost["total_cmv_brl"])
    per_portion = numeric(cost["cmv_per_portion_brl"])
    check(total == EXPECTED_TOTAL, "Batch CMV differs from the original R$ 35.59300")
    check(
        per_portion == total / Decimal(15), "Per-portion CMV disagrees with batch CMV"
    )
    check(
        numeric(cost["supplemental_purchase_total_brl"]) == 0,
        "Unexpected additional purchases",
    )
    check(
        numeric(cost["budget_remaining_after_purchase_brl"]) == 80,
        "Budget balance differs from R$ 80",
    )
    check(cost.get("within_budget") is True, "Purchase budget was exceeded")

    check(
        numeric(pricing["total_cmv_brl"]) == total, "Pricing CMV differs from cost tool"
    )
    check(
        numeric(pricing["cmv_per_portion_brl"]) == per_portion,
        "Per-portion CMV differs",
    )
    check(
        pricing.get("cmv_complete") is False,
        "Pricing failed to mark missing ingredients",
    )
    check(len(pricing.get("pending_cost_items", [])) == 2, "Pending costs were lost")
    check(pricing.get("servings") == 15, "Pricing used incorrect serving count")
    check(
        numeric(pricing["platform_fee_rate"]) == Decimal("0.10"),
        "Incorrect platform fee",
    )
    check(pricing.get("currency") == "BRL", "Incorrect currency")
    check(
        numeric(pricing["break_even_price_per_portion_brl"]) == Decimal("2.64"),
        "Incorrect ingredient-only break-even price",
    )
    check(pricing["delivery_pricing"].get("batch") is not None, "Batch pricing missing")

    scenarios = pricing["delivery_pricing"]["per_portion"]["scenarios"]
    check(len(scenarios) == 3, "Expected three selling-price scenarios")
    prices = []
    for index, item in enumerate(scenarios):
        price = numeric(item["selling_price"])
        prices.append(price)
        if index >= len(EXPECTED_PRICES):
            continue
        check(price == EXPECTED_PRICES[index], f"Scenario {index + 1} price differs")
        check(
            numeric(item["target_margin_on_gross_price"]) == EXPECTED_MARGINS[index],
            f"Scenario {index + 1} margin differs",
        )
        check(
            numeric(item["platform_fee"]) == price * Decimal("0.10"),
            f"Scenario {index + 1} platform fee differs",
        )
        check(
            numeric(item["net_revenue"]) == price * Decimal("0.90"),
            f"Scenario {index + 1} net revenue differs",
        )
        check(
            numeric(item["estimated_profit"])
            == numeric(item["net_revenue"]) - per_portion,
            f"Scenario {index + 1} contribution differs",
        )

    finals = [e for e in events if e.get("type") == "result"]
    check(bool(finals), "Missing Hermes final result event")
    answer = str(finals[-1].get("text", "")) if finals else ""
    if finals:
        check(finals[-1].get("exit_code") == 0, "Hermes exited with error")
    for label, amount in (
        ("batch CMV", total),
        ("per-portion CMV", per_portion),
        ("ingredient break-even", Decimal("2.64")),
        *((f"scenario {i + 1}", p) for i, p in enumerate(prices[:3])),
    ):
        check(f"R$ {money(amount)}" in answer, f"Final answer does not report {label}")
    check("INCOMPLETO" in answer.upper(), "Final answer did not flag incomplete costs")

    return {
        "passed": not errors,
        "scenario": "approved_galinhada_replay",
        "verification_method": "read-only replay of original trace inputs against current workbook",
        "servings": 15,
        "batch_cmv_brl_exact": str(total),
        "per_portion_cmv_brl_exact": str(per_portion),
        "supplemental_purchase_brl": str(cost["supplemental_purchase_total_brl"]),
        "cmv_complete": pricing["cmv_complete"],
        "break_even_ingredient_only_brl": pricing["break_even_price_per_portion_brl"],
        "scenarios_brl": [str(price) for price in prices],
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    # Imported outside the try block: the except clause below references
    # ToolError, so a failed import must surface as itself, not as an
    # UnboundLocalError raised while matching the except tuple.
    from fastmcp.exceptions import ToolError

    try:
        events = load_events(args.trace)
        cost_args = extract_call(events, COST_TOOL)
        pricing_args = extract_call(events, PRICING_TOOL)
        cost, pricing = asyncio.run(replay_tools(cost_args, pricing_args))
        report = audit(events, cost, pricing)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        UnicodeError,
        ToolError,
    ) as exc:
        report = {
            "passed": False,
            "scenario": "approved_galinhada_replay",
            "errors": [str(exc)],
        }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
