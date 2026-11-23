# Hermes MCP Trace Evals (L1 trace grader, deterministic)

The `grade_pantry_summary.py` grader scores a **real Hermes Agent execution trace** captured through `hermes chat --format stream-json`. Grading is fully offline: no LLM or external API is called.

## Usage (PowerShell)

```powershell
$trace = Get-ChildItem "$env:TEMP\ifood-sabor-maria-evals\mcp-smoke-*.jsonl" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName
uv run python evals/grade_pantry_summary.py $trace
uv run pytest tests/evaluation -q
```

Expected success: `"passed": true`, `"tool_calls": 1`, no errors, process exit code zero.

## What it checks

- The agent invoked `mcp__sabor_da_maria__get_pantry_summary`.
- The tool returned successfully, matched by `tool_call_id`.
- The nested MCP response can be parsed, matching the known provided workbook snapshot: 37 ingredients, R$663.39 historical spending, R$80.00 remaining budget.
- A final `result` event exists with exit code zero.
- Final reported currency amounts and ingredient count match that snapshot.

## Boundaries and limitations

- This is only the pantry-summary smoke scenario, **not a complete challenge evaluation**.
- Financial values are intentionally fixed for the challenge workbook snapshot. Update the baseline if the workbook changes.
- This grader does not prove equipment discovery, recipe search provenance, approval order, recipe CMV, pricing, session continuity, or semantic interpretation of amounts; those need separate tests.
- No raw agent trace is committed: local session traces may include private data or prompts. Keep them outside Git.
- The Hermes run in the current profile was not fully isolated; use a disposable profile before running a broader L1 suite.
- Cost and latency are observed from Hermes's `result` event, not synthesized.

## Windows PowerShell trace encoding

The grader accepts UTF-8 (with or without BOM) and UTF-16 with a BOM. Windows PowerShell 5.1 may write Tee-Object output as UTF-16 LE, so manual conversion is not required.
