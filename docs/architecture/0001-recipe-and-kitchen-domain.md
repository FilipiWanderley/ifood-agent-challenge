# Recipe and Kitchen Domain Model

## Status

Implemented on `feat/recipe-domain-model`; extended by the persisted workflow in ADR 0002.

## Context

The challenge requires the agent to discover recipe requirements and verify that the cook can produce a selected recipe before ingredient purchases or pricing are treated as approved. Prompt-only instructions are not a reliable enforcement boundary.

## Decision

Represent recipes, recipe sources, kitchen capabilities, and feasibility assessments as immutable Pydantic domain models. Evaluate feasibility with deterministic Python rules, independently of Hermes, the LLM, MCP transport, and workbook I/O.

The evaluator returns one of three statuses:

- `feasible`: all modeled requirements are known and satisfied;
- `infeasible`: at least one confirmed operational blocker exists;
- `needs_information`: no confirmed blocker exists, but one or more required facts are unknown.

Unknown inventory (`None`) is distinct from a known-empty inventory (`()`). This prevents the system from interpreting missing answers as proof that the cook lacks equipment or skills.

### Equipment inventory completeness

`KitchenProfile.equipment` and `equipment_inventory_complete` distinguish an incomplete inventory from a confirmed exhaustive inventory:

| State | Meaning | Evaluator behaviour for an unlisted required item |
| --- | --- | --- |
| `equipment is None` | No equipment facts have been elicited. | `needs_information` |
| `equipment_inventory_complete` is `None` or `False` | The list may be incomplete. | `needs_information` |
| `equipment_inventory_complete=True` | Dona Maria confirmed the list is exhaustive. | `infeasible` (`equipment_missing`) |
| An item is listed with `available=False` | The item is explicitly unavailable. | `infeasible` (`equipment_unavailable`) |
| An item is listed with `available=None` | Its availability is unknown. | `needs_information` |

`equipment_inventory_complete=True` requires an equipment list. No equipment substitute is inferred.

### Tri-state recipe requirements

The same distinction applies to the recipe side. `Recipe.required_equipment`, `Recipe.required_skills`, `Recipe.required_utilities` and `Recipe.storage_requirements` are tri-state and default to `None`:

| Value | Meaning | Evaluator behaviour |
| --- | --- | --- |
| `None` (default) | Not yet researched or confirmed. | One `missing_information` issue per category, including `recipe_storage_requirements_unknown` (subject = field name). |
| `()` | Explicitly checked; the recipe requires nothing in this category. | No issue; the matching kitchen facts are not consulted. |
| non-empty tuple | Confirmed requirements. | Existing item-by-item validation against `KitchenProfile`. |

Recipe discovery is incremental, so an absent answer must never be read as "requires nothing". Previously these fields defaulted to `()`, which let an incompletely researched recipe be classified as `feasible`. An unknown category can only produce `needs_information` (or be overridden by a confirmed blocker elsewhere, in which case the unknown is still listed).

Compatibility impact: callers that omitted these fields previously got `feasible` (if everything else was satisfied) and now get `needs_information`. Callers that must assert "no requirements" have to pass `()` explicitly. JSON serialization emits `null` for unknown and `[]` for confirmed-empty, and both round-trip unchanged.

### Operational feasibility is not recipe approval

`feasible` means only that, given the facts known about the kitchen, the cook can operationally produce the recipe at the target volume. It does **not** mean:

- the pantry holds every ingredient (pantry completeness);
- the ingredient purchase is approved;
- the recipe is ready for costing — e.g. unquantified ingredients are reported only as warnings and still block a complete CMV.

Complete recipe approval is a separate, later gate that combines the feasibility assessment with pantry, purchasing and costing readiness. It is not implemented by `FeasibilityEvaluator`.

## Rules

- Every recipe candidate must include at least one HTTP(S) source.
- Recipe yield describes servings per batch.
- Ingredient quantity and unit are either both present or both absent. Missing quantities are exposed for downstream costing gates.
- Equipment requirements may specify minimum quantity and capacity.
- Capacity comparison is performed only when units match exactly. Unsupported conversions produce `needs_information`, not a guessed result.
- Required skills and utilities are checked against explicitly known kitchen facts.
- Recipe requirement categories left as `None` are missing information, never evidence of feasibility; only `()` means "none required".
- A `ProductionPlan` uses the source yield as the immutable batch size and calculates `ceil(target_servings / recipe.servings)` whole batches. It records target, batch count, total production and surplus, and multiplies every quantified ingredient by the same batch count.
- A surplus requires `ProductionConfirmation` for that exact batch count. A partial batch is never assumed and overproduction is never silently accepted.
- A multi-batch duration is never extrapolated from the single-batch source time. Dona Maria must confirm the total time for all batches. A confirmation for a different batch count is ignored and reported as stale/mismatched information.
- Refrigerator and freezer capacity are free space in liters. Recipe storage requirements must specify the volume per batch; the evaluator checks one-batch adequacy and whether all batches fit together. If one batch fits but all batches do not, the result is unresolved rather than a guessed schedule.
- `minimum_people` remains unknown when the source does not state it; it is never defaulted to one.
- Confirmed blockers take status precedence over unknown facts; unknowns are still retained in the assessment for auditability.

## Consequences

The model can be tested without an LLM or a live MCP server. The evaluator does not prove food safety, recipe taste, equipment scheduling, simultaneous stove occupancy, cooling rate, temperature control, shelf life or storage rotation. Refrigeration checks are volume-based and depend on user/source-provided free capacity and recipe volume. Batch timing for multiple batches requires an explicit user confirmation rather than a mathematical extrapolation.

## Next step

Implemented in [0002 — Official planning workflow, state machine and SQLite persistence](0002-planning-workflow-and-persistence.md).
