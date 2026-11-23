# Audit of the approved-recipe financial turn

This offline grader replays the recorded `calculate_recipe_cost` and `calculate_delivery_pricing` inputs from an existing Hermes `stream-json` trace through the in-process FastMCP `Client`, repairing only recognized reversible Windows console text-encoding corruption where needed.

It does **not** call the LLM and does **not** modify the Excel workbook. It accepts UTF-8/UTF-8-BOM and UTF-16-BOM traces produced by Windows PowerShell.

It checks the 15-serving galinhada scenario, tool input parity, missing ingredient flags, 10% delivery fee, purchase budget, per-serving CMV, break-even, the three 15%/25%/35% pricing scenarios and the agent's final reported values. A failed assertion yields a JSON `passed: false` report and nonzero exit code.

**Limitation:** replay executes against the *current* workbook and source code. This independently reproduces the outputs, but it does not recover truncated historical tool_result data or prove the exact same binary/model response was used during the original run. The workbook should therefore remain unchanged between the trace and replay.

Command (from project root):

```
uv run python evals/audit_financial_trace.py "C:\path\to\approved-recipe-pricing-TIMESTAMP.jsonl"
```

Unit checks:

```
uv run pytest tests/evaluation/test_financial_trace_audit.py -q
```

## Windows console encoding recovery

Some Hermes `stream-json` captures on Windows contain CP850-decoded UTF-8 in
`tool_use.input` string values: `Óleo de soja` can appear as `├ôleo de soja`
and `Açafrão em pó (cúrcuma)` can appear as `A├ºafr├úo em p├│ (c├║rcuma)`.
The auditor reverses that specific reversible text corruption **only in its
in-memory replay argument copies**. It does not rewrite the original trace,
rename catalog ingredients, edit the Excel workbook, or change financial logic.
This is a reconstruction of the recorded input's intended Unicode, not
recovery of truncated original MCP tool output. Run the real tests and replay
against the same workbook and source to verify the result.
