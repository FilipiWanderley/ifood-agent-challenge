"""Offline mutation tests of the trace grader; no Hermes/API calls needed."""

import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "evals" / "grade_pantry_summary.py"
spec = importlib.util.spec_from_file_location("grade_pantry_summary", MODULE_PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def events():
    data = {
        "ingredient_count": 37,
        "historical_purchase_total_brl": "663.39",
        "remaining_budget_brl": "80.00",
    }
    return [
        {"type": "tool_use", "name": mod.TOOL, "tool_call_id": "call1", "input": {}},
        {
            "type": "tool_result",
            "name": mod.TOOL,
            "tool_call_id": "call1",
            "output": json.dumps({"result": json.dumps(data)}),
            "is_error": False,
        },
        {
            "type": "result",
            "exit_code": 0,
            "text": "37 ingredients, historical R$ 663,39, remaining R$ 80,00.",
            "duration_ms": 120,
            "tokens": {"total": 250},
        },
    ]


def test_happy_path():
    assert mod.grade(events())["passed"]


def test_missing_tool():
    trace = events()[2:]
    assert not mod.grade(trace)["passed"]


def test_missing_result():
    trace = [events()[0], events()[2]]
    assert any("Missing MCP" in e for e in mod.grade(trace)["errors"])


def test_tool_error():
    trace = events()
    trace[1]["is_error"] = True
    assert not mod.grade(trace)["passed"]


def test_wrong_financial_value():
    trace = events()
    trace[1]["output"] = json.dumps(
        {
            "result": json.dumps(
                {
                    "ingredient_count": 37,
                    "historical_purchase_total_brl": "663.39",
                    "remaining_budget_brl": "90.00",
                }
            )
        }
    )
    assert not mod.grade(trace)["passed"]


def test_hallucinated_final_amount():
    trace = events()
    trace[-1]["text"] = "37 ingredients, historical R$ 663,39, remaining R$ 200,00."
    assert not mod.grade(trace)["passed"]


def test_bad_exit_code():
    trace = events()
    trace[-1]["exit_code"] = 1
    assert not mod.grade(trace)["passed"]


def test_load_trace_ignores_session_status_lines(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        "Session: local-test\n"
        + "\n".join(json.dumps(e) for e in events())
        + "\nsession_id: local-test\n",
        encoding="utf-8",
    )
    assert len(mod.load_trace(path)) == 3


def test_load_trace_rejects_corrupt_json(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    try:
        mod.load_trace(path)
    except ValueError as exc:
        assert "Invalid JSONL" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_load_trace_accepts_windows_powershell_utf16(tmp_path):
    path = tmp_path / "trace-utf16.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events()), encoding="utf-16")
    assert len(mod.load_trace(path)) == 3


def test_load_trace_accepts_utf8_bom(tmp_path):
    path = tmp_path / "trace-utf8-bom.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events()), encoding="utf-8-sig")
    assert len(mod.load_trace(path)) == 3


def test_load_trace_rejects_invalid_utf16(tmp_path):
    path = tmp_path / "trace-invalid.jsonl"
    path.write_bytes(b"\xff\xfe\x00")
    try:
        mod.load_trace(path)
    except ValueError as exc:
        assert "Invalid trace encoding" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid encoding")
