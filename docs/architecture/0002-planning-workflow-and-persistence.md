# 0002 — Official planning workflow, state machine and SQLite persistence

## Status

Implemented on `feat/recipe-domain-model` (Phase 2).

## Context

Phase 1 added the immutable `Recipe`, `KitchenProfile` and
`FeasibilityEvaluator` domain models. The challenge needs the agent to keep
Dona Maria from buying ingredients for a dish she cannot produce, and to
calculate CMV and price only after she accepts the dish. Instructions in a
prompt cannot enforce that order. The workflow state also has to survive
restarts of the MCP server process.

## Decision

### Layers

| Layer | Module | Responsibility |
| --- | --- | --- |
| Domain | `domain/workflow.py` | `CandidateStatus`, allowed-transition table, `SourceEvidence` |
| Domain | `domain/recipe.py`, `kitchen.py`, `production.py`, `feasibility.py`, `workflow.py` | Recipe requirements, equipment knowledge, whole-batch production plan and deterministic feasibility |
| Domain (existing) | `domain/costing.py`, `domain/pantry.py` | Existing financial and pantry models/calculators, reused without duplicated formulas |
| Application | `application/readiness.py` | Feasibility + pantry comparison + quote and budget checks |
| Application | `application/planning.py` | `PlanningService` use cases, versioning, approval binding |
| Infrastructure | `infrastructure/persistence/sqlite.py` | `sqlite3` repository, schema, transactions |
| Transport | `infrastructure/mcp/workflow_tools.py` | Seven thin MCP tools; no business rules |

### State machine

```
DISCOVERED -> NEEDS_INFORMATION | EVALUATED
EVALUATED  -> INFEASIBLE | FEASIBLE
FEASIBLE   -> AWAITING_APPROVAL
AWAITING_APPROVAL -> APPROVED | REJECTED
APPROVED   -> COSTED -> PRICED
any non-DISCOVERED (except the transient EVALUATED) -> DISCOVERED  (invalidation)
```

- Any transition not in the table raises `InvalidTransitionError`.
- One service call can take several hops. For example, evaluation can go
  `DISCOVERED -> EVALUATED -> FEASIBLE -> AWAITING_APPROVAL`. Every hop is
  validated and recorded as a `workflow_events` row. The candidate row is
  written once, and its `version` increases by one per operation.
- `EVALUATED` and `COSTED` are transient: they only exist inside a single
  transaction and are never the final persisted state.
- Re-evaluating a `NEEDS_INFORMATION` or `INFEASIBLE` candidate first goes
  back to `DISCOVERED`.

### Production plan and readiness (evaluation)

- `ProductionPlan` is the single source of truth for a day's production. It
  preserves source yield as batch size, computes the minimum whole-batch count,
  total produced servings and surplus, and scales every ingredient by that
  exact count.
- Surplus requires an explicit `ProductionConfirmation` bound to the current
  source yield, daily target and batch count. A multi-batch plan also requires
  a user-confirmed total duration;
  time is never multiplied linearly from a source's single-batch duration.
- The confirmation is stored on the candidate, included in the approval
  fingerprint, returned by `get_planning_status`, and persisted in SQLite.
  Revising the recipe or kitchen invalidates the prior evaluation/approval.
- Equipment not listed in an incomplete inventory is unknown. An item is
  unavailable only when explicitly marked so or when the inventory is
  confirmed complete.
- Refrigerator/freezer availability and free capacity (liters) are modeled.
  Recipe storage requirements carry an explicit per-batch volume. If a batch
  fits but all planned batches do not fit together, storage remains unresolved.
- `minimum_people` and recipe storage requirements remain unknown unless
  stated/confirmed; they are never defaulted.


`evaluate_readiness` returns `feasible` only when **all** of these hold:

1. `FeasibilityEvaluator` reports the recipe operationally feasible.
   Unknown equipment, skills, utilities, time or people count as
   *missing information*, never as feasibility.
2. Every ingredient has a quantity and unit. The only exception is when the
   user explicitly sets `allow_partial_cmv`. The unquantified items then
   become `pending_cost_items`, and the final result reports
   `cmv_complete = false`.
3. Units are supported and compatible with the pantry dimension.
4. Every shortfall against pantry stock has a supplemental quote with a
   positive price, a string value and an http(s) source URL.
5. `RecipeCostCalculator` accepts the inputs, and its
   `supplemental_purchase_total` fits within
   `remaining_budget (R$ 80) − purchases already approved for other
   candidates in the same session`.

Purchase totals, package counts and the budget figures come from the existing
`RecipeCostCalculator`. The application layer adds no CMV or pricing formula.
The pantry comparison only converts units (`normalize_measure`) and subtracts
stock from the requirement to show what is missing. Evaluation does not show
CMV or prices.

### Approval binding and invalidation

- `approve_recipe_candidate` is only valid in `AWAITING_APPROVAL`. It
  requires the current `expected_candidate_version` and a non-empty
  `user_confirmation` quoting Dona Maria. It stores an `ApprovalRecord` with
  the version, the decision, the approved purchase total and an **input
  fingerprint**: SHA-256 of recipe, quotes, `allow_partial_cmv`, production
  confirmation, kitchen profile and a digest of the pantry catalog.
- The shared budget and full pantry allocation are rechecked at approval time
  inside the same `BEGIN IMMEDIATE` transaction. A later candidate cannot
  spend the same R$ 80 or reuse inventory reserved by an earlier approval.
  A change since evaluation, or a budget commitment that makes the plan
  unaffordable, invalidates the evaluation before approval can proceed.
- Repeating the same decision for the same version is idempotent: it returns
  the stored record and writes no new event. A different decision for the
  same version is rejected as stale.
- These events send candidates back to `DISCOVERED` and clear evaluation,
  approval and calculation:
  - a revised recipe or quotes (`register_recipe_candidate` with
    `candidate_id`);
  - a kitchen-profile update, which invalidates every candidate in the
    session;
  - a pantry workbook or shared stock reservation change detected at approval
    time: the evaluation is reset before a new explicit decision;
  - a later input/catalog change detected at calculation time: the approval
    is reset, with the invalidation committed before the error is returned.
- `calculate_approved_recipe` loads persisted state and checks, in order:
  - the version;
  - status `APPROVED`;
  - that the approval belongs to the previous version;
  - the fingerprint, including the production confirmation;
  - that the recalculated purchase total equals the approved one;
  - that the approved production batch count still matches the stored plan.

  Only then does it call `RecipeCostCalculator` and
  `DeliveryPricingCalculator`. A repeated call on a `PRICED` candidate
  returns the stored result.

### Persistence

- Uses only standard-library `sqlite3`. Tables: `planning_sessions`,
  `recipe_candidates`, `workflow_events`. The schema version is tracked
  with `PRAGMA user_version`. Schema v2 added `production_confirmation_json`;
  schema v3 enforces source-yield/target-bound confirmations and session pantry
  reservations. Existing v1/v2 databases migrate additively. Any candidate
  evaluated before v3 is reset to `DISCOVERED`, with evaluation, approval and
  calculation cleared and a migration event. Legacy confirmations are never
  accepted without their current source yield and target being re-confirmed.
- `PRAGMA foreign_keys = ON`, `CHECK` constraints on status, flags and id
  lengths, and `UNIQUE (session_id, recipe_id)`.
- Each service operation is one `BEGIN IMMEDIATE` transaction on its own
  connection, so read-check-write is serialized across threads and
  processes. Every `UPDATE` also carries `WHERE version = ?` (optimistic
  lock). Every value is passed as a SQL parameter.
- Location: the `SABOR_DA_MARIA_DB_PATH` environment variable if set. A
  relative value is resolved against the current working directory.
  Otherwise `%LOCALAPPDATA%\sabor-da-maria-agent\planning.sqlite3` on
  Windows, or `$XDG_DATA_HOME` / `~/.local/share/...` elsewhere.
- The database is created lazily on the first workflow tool call, never at
  module import. Tests use temporary databases.
- Financial values are stored as JSON strings of `Decimal`s
  (`model_dump(mode="json")`), never as floats.

### Source provenance

Hermes `web_search` results are stored as `SourceEvidence`: title, URL,
optional excerpt, agent-reported `retrieved_at`, server `recorded_at`, and
`discovery_method = "hermes_web_search"`. By default,
`verification_status = "unverified_search_result"`. An optional
`manual_review` input records a named person's attestation that the page was
opened and read, setting `verification_status = "manually_reviewed"`.
That attestation is not authenticated by the server; the server never fetches
the URL and never claims automated verification.

### Compatibility interfaces

`get_pantry_summary`, `get_pantry_inventory`, `calculate_recipe_cost` and
`calculate_delivery_pricing` keep their exact signatures and outputs, because
the historical financial replay audit (`evals/audit_financial_trace.py`)
depends on them. `calculate_recipe_cost` and `calculate_delivery_pricing`
are **compatibility interfaces**: they do not check feasibility or approval
and are not part of the official conversational workflow. A contract test
asserts that `calculate_approved_recipe` returns exactly the same
`delivery_pricing` as `calculate_delivery_pricing` for the same inputs.

## Consequences and limitations

- **Unverified sources.** URLs, titles and excerpts are evidence reported by
  the agent, not verified content.
- **CMV covers ingredients only.** Packaging, gas, electricity, labour and
  taxes are excluded. The result lists them in `excluded_costs`. The 10%
  platform fee is applied by the existing pricing policy.
- **Inventory reservation within a session.** Once a recipe is approved, its
  full production plan reduces the available pantry stock for later recipes.
  SQLite approval event IDs preserve the consumption order. Earlier approvals
  keep their allocation even when subsequent recipes are approved. Purchases
  are costed per recipe and are not automatically pooled into pantry stock.
  Reservations from separate planning sessions are not shared.
- **Quotes are not checked against the supplier.** Price, package and URL
  are validated structurally only.
- **The feasibility model is limited.** Stove occupancy and parallel cooking
  are not modeled. Refrigeration is checked only by user/source-provided free
  volume; temperature, cooling rate, shelf life and rotation are not modeled.
  Multi-batch time must be confirmed by Dona Maria, not extrapolated.
- **Approval can be forged.** `user_confirmation` is a recorded quote, not
  an authenticated signature. The skill must call the tool only after an
  explicit "yes" in the conversation.
- **Single-host database.** SQLite is a good fit for one local Hermes
  installation. It is not designed for a shared multi-host deployment.
- **Clock-based ordering.** Candidate listing order uses `created_at`, then
  `candidate_id`.
