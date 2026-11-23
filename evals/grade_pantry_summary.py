"""Deterministic grader for the pantry-summary Hermes JSONL smoke scenario."""

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

TOOL = "mcp__sabor_da_maria__get_pantry_summary"
EXPECTED_COUNT = 37
EXPECTED_HISTORY = Decimal("663.39")
EXPECTED_BUDGET = Decimal("80.00")


def load_trace(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    payload = path.read_bytes()
    encoding = "utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        lines = payload.decode(encoding).splitlines()
    except UnicodeError as exc:
        raise ValueError(f"Invalid trace encoding: {exc}") from exc
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith(("Session:", "session_id:")):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL on line {line_number}: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise TypeError(f"Expected JSON object on line {line_number}")
        events.append(event)
    return events


def parse_summary(output: Any) -> dict[str, Any]:
    data = json.loads(output) if isinstance(output, str) else output
    if isinstance(data, dict) and "result" in data:
        data = json.loads(data["result"]) if isinstance(data["result"], str) else data["result"]
    if not isinstance(data, dict):
        raise TypeError("MCP summary must be a JSON object")
    return data


def _brl_number(raw: str) -> Decimal:
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid BRL value: {raw}") from exc


def grade(events: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    tool_uses = [e for e in events if e.get("type") == "tool_use" and e.get("name") == TOOL]
    tool_results = {
        e.get("tool_call_id"): e
        for e in events
        if e.get("type") == "tool_result" and e.get("name") == TOOL
    }
    if not tool_uses:
        errors.append("Agent did not call the pantry-summary MCP tool")

    summaries = []
    for use in tool_uses:
        call_id = use.get("tool_call_id")
        result = tool_results.get(call_id)
        if not result:
            errors.append(f"Missing MCP tool_result for call {call_id}")
            continue
        if result.get("is_error"):
            errors.append(f"MCP call failed: {call_id}")
            continue
        try:
            summaries.append(parse_summary(result.get("output")))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"MCP result cannot be decoded: {exc}")

    if summaries:
        summary = summaries[-1]
        try:
            count = int(summary["ingredient_count"])
            history = Decimal(str(summary["historical_purchase_total_brl"]))
            budget = Decimal(str(summary["remaining_budget_brl"]))
            if (count, history, budget) != (EXPECTED_COUNT, EXPECTED_HISTORY, EXPECTED_BUDGET):
                errors.append("Pantry summary does not match baseline workbook financial values")
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            errors.append(f"Invalid pantry summary fields: {exc}")

    final_events = [e for e in events if e.get("type") == "result"]
    if not final_events:
        errors.append("Missing final result event")
    else:
        final = final_events[-1]
        if final.get("exit_code") != 0:
            errors.append(f"Hermes exit_code is not zero: {final.get('exit_code')}")
        answer = str(final.get("text", ""))
        if not re.search(r"\b37\b", answer):
            errors.append("Final answer omitted the 37-ingredient count")
        money_strings = re.findall(r"R\$\s*((?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}|\d+\.\d{2})", answer)
        money_values = {_brl_number(value) for value in money_strings}
        if money_values != {EXPECTED_HISTORY, EXPECTED_BUDGET}:
            errors.append("Final answer BRL amounts do not match MCP summary")

    last = final_events[-1] if final_events else {}
    return {
        "passed": not errors,
        "scenario": "pantry_summary_smoke",
        "tool_calls": len(tool_uses),
        "duration_ms": last.get("duration_ms"),
        "tokens": last.get("tokens"),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    try:
        report = grade(load_trace(args.trace))
    except (OSError, ValueError) as exc:
        report = {"passed": False, "scenario": "pantry_summary_smoke", "errors": [str(exc)]}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
