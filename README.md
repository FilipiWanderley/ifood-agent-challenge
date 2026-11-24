# Sabor da Maria Agent

An agentic AI application for intelligent recipe planning, pantry management, ingredient costing, and budget-aware shopping recommendations.

Hermes Agent is the conversational layer. Its Dona Maria skill lives in `.hermes/skills/dona-maria-consultora/SKILL.md`. Deterministic Python code, exposed through the `sabor-da-maria` MCP server, enforces workflow order, feasibility, budget and every financial calculation.

## Technology

- Python 3.12
- uv
- FastMCP
- Pydantic
- OpenPyXL
- SQLite (standard-library `sqlite3`)
- Pytest
- Ruff
- Mypy
- Pyright

## Setup

```powershell
uv sync --all-groups
```

Start the MCP server (stdio):

```powershell
uv run sabor-da-maria-agent
```

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SABOR_DA_MARIA_WORKBOOK` | `data/raw/despensa_dona_maria.xlsx` | Pantry workbook (read-only) |
| `SABOR_DA_MARIA_DB_PATH` | `%LOCALAPPDATA%\sabor-da-maria-agent\planning.sqlite3` (Windows) or `~/.local/share/sabor-da-maria-agent/planning.sqlite3` | Planning-workflow database |

The database is created on the first workflow tool call, never at import. It holds planning state only (sessions, candidates, approvals, events) and no credentials. `*.sqlite3` files are git-ignored.

## Official conversational workflow

| Step | MCP tool | Deterministic gate |
| --- | --- | --- |
| 1 | `start_planning_session` | Creates a persisted session, optionally with confirmed kitchen facts |
| 2 | `update_kitchen_profile` | Merges confirmed equipment, inventory completeness, skills, time, people, utilities and free refrigerator/freezer capacity. Invalidates every candidate in the session |
| 3 | `register_recipe_candidate` | Recipe found via Hermes `web_search`, with source evidence, operational/storage requirements, optional production confirmation and supplemental quotes |
| 4 | `evaluate_recipe_candidate` | Feasibility, pantry comparison, missing ingredients, quotes and the shared R$ 80 budget |
| 5 | `approve_recipe_candidate` | Dona Maria's explicit decision, tied to the exact candidate version |
| 6 | `calculate_approved_recipe` | CMV and delivery pricing (10% fee), only for an approved and unchanged candidate |
| — | `get_planning_status` | Session, candidates, versions and audit events |

State machine:

```
DISCOVERED -> NEEDS_INFORMATION | EVALUATED -> INFEASIBLE | FEASIBLE -> AWAITING_APPROVAL -> APPROVED | REJECTED
APPROVED -> COSTED -> PRICED
```

Behaviour of the gates:

- Unknown facts are never treated as feasible. Equipment absent from an incomplete inventory is unknown; it is unavailable only when explicitly marked unavailable or when Dona Maria confirms the inventory is complete.
- A production plan uses whole batches at the source recipe yield. The batch count is `ceil(target servings / source yield)`. Any surplus must be explicitly confirmed; partial batches are never assumed.
- Ingredient checks, quotes, purchase totals, budget checks and final CMV all use the same complete-production-plan quantities. A 12-serving recipe for a 15-serving target means two 12-serving batches (24 produced), not 15 servings and not one batch's ingredients.
- Multi-batch cooking time is never multiplied linearly. `production_confirmation` records Dona Maria's confirmed batch count and, when needed, the total time for all batches.
- Refrigerator/freezer availability and free capacity are modeled separately. A recipe's storage volume must be explicitly supplied; capacity is never inferred from servings.
- Changing the recipe, production confirmation, quotes, kitchen profile or pantry returns the candidate to `DISCOVERED`. That discards the evaluation and approval.
- Invalid transitions and stale versions are rejected.

Design and rationale: [docs/architecture/0002-planning-workflow-and-persistence.md](docs/architecture/0002-planning-workflow-and-persistence.md).

### Compatibility interfaces

`get_pantry_summary` and `get_pantry_inventory` remain the pantry tools.

`calculate_recipe_cost` and `calculate_delivery_pricing` keep their original contracts for the historical financial replay audit. They are **compatibility interfaces**: they do not check feasibility or approval and are not the official workflow. The official `calculate_approved_recipe` reuses the same calculators. A contract test asserts that both paths produce identical pricing.

## Quality Gates

```powershell
uv run ruff check src tests evals
uv run mypy src
uv run pyright
uv run pytest tests/unit tests/integration tests/evaluation tests/contract -q
```

Financial replay audit of a recorded Hermes trace (see `evals/README_financial_audit.md`):

```powershell
uv run python evals/audit_financial_trace.py "$env:TEMP\ifood-sabor-maria-evals\approved-recipe-pricing-<timestamp>.jsonl"
```

## Staff-level data integrity invariants

- Production confirmations are bound by the server to the exact source recipe yield and daily target, not just the batch count. A changed target requires new confirmation even if the number of batches is unchanged.
- A kitchen skill missing from an incomplete skills list means **unknown**, not **unavailable**. Only a confirmed complete skills inventory turns that absence into a blocker.
- Even a recipe requiring one person needs the reported available-people count before being deemed feasible.
- The approval transaction rechecks the real pantry and the reservations committed by other approved recipes. A stale evaluation is invalidated; approval must be requested again after re-evaluation.
- Schema v3 conservatively resets decisions created under v1/v2 gates. The agent should retrieve session status and re-evaluate instead of trusting old approvals.

## Limitations

- **Recipe sources are not verified.** The server stores what `web_search` returned and never fetches URLs. Evidence is labelled `unverified_search_result`.
- **CMV covers ingredients only.** Packaging, gas, electricity, labour and taxes are excluded and are listed in `excluded_costs`.
- **Partial CMV must be explicit.** Ingredients without quantities ("sal a gosto") block evaluation unless Dona Maria explicitly accepts a partial CMV (`allow_partial_cmv`). The result then reports `cmv_complete = false`.
- **Shared pantry stock reservations.** Approved recipes reserve the workbook inventory within the same planning session. Later candidates see the unreserved stock, and prior approvals retain their allocation. Reservation order follows immutable SQLite approval-event IDs, not timestamps. Purchased packages are recipe-specific and are not automatically added as shared stock. Separate sessions do not share reservations.
- **Quotes are checked for structure only.** Supplier quotes must be well-formed (http(s) URL, positive string values) but are not confirmed with the supplier.
- **Approval is recorded, not authenticated.** It quotes the user's words in the conversation and relies on the skill to ask first.
- **Production scheduling is conservative.** Batches are planned as whole source-yield batches; stove occupancy and parallel cooking across burners are not modeled. Total time for multiple batches must be confirmed by Dona Maria rather than extrapolated.
- **Cold-storage checks are volume-based and partial.** The system compares explicitly reported free liters with an explicitly reported recipe volume per batch. It does not model temperature, cooling rate, shelf life, food-safety limits or storage rotation. If the combined volume of all batches exceeds the reported free capacity, the result remains unresolved until the storage plan is clarified.
- **Equipment inventory completeness is explicit.** A missing item is not treated as unavailable unless the inventory is marked complete or the item is explicitly unavailable.

## Project Structure

- docs/: requirements, architecture and technical decisions
- data/: challenge datasets
- src/sabor_da_maria_agent/domain: recipe, kitchen, production, feasibility, workflow, pantry, costing
- src/sabor_da_maria_agent/application: planning service and readiness evaluation
- src/sabor_da_maria_agent/infrastructure: Excel loader, SQLite persistence, MCP server
- tests/: unit, integration, contract and evaluation tests
- evals/: offline graders for recorded Hermes traces
- scripts/: development utilities

## Current Progress

- Pantry workbook ingestion
- Ingredient normalization
- Purchase cost reconciliation
- Input validation
- Recipe, kitchen, production-plan and feasibility domain models
- Whole-batch production planning with explicit surplus/time confirmation
- Ingredient readiness that preserves unsupported units and checks full-plan quantities
- Refrigerator/freezer availability and free-capacity checks
- Persisted planning workflow with approval-gated CMV and pricing
- Automated tests and an offline financial trace audit

## Architecture choices required by the challenge

### Model selection and conversational reasoning

The demo was run with Hermes Agent and the `claude-opus-5-5` model through the Anthropic provider. The model is **replaceable**: it researches recipe candidates with `web_search`, asks Dona Maria for missing facts, translates her answers into validated tool inputs and explains the findings. It does **not** decide mathematical feasibility or perform production financial arithmetic on its own. Provider credentials and Hermes user-level settings are intentionally not checked into Git.

The deliberate trade-off is to use a strong general-purpose LLM for flexible dialogue while making critical decisions reproducible in Python, independently of model sampling. A model change may affect conversation quality and tool selection, but should not change the calculation semantics of a given validated tool call.

### Context files and skills

- `docs/requirements/desafio-senior-ai-engineer.md` is the preserved assignment and acceptance criteria.
- `.hermes/skills/dona-maria-consultora/SKILL.md` is the project-local Hermes skill: conversational instructions, discovery questions, tool calling rules, explicit human approval, source attribution and limits of the financial model.
- `README.md`, `docs/architecture/` and `docs/decisions/` explain reproducibility, separation of responsibilities and implementation trade-offs for reviewers. These files are documentation, **not** a hidden database of kitchen facts.
- The workbook `data/raw/despensa_dona_maria.xlsx` is the source of truth for pantry quantities and historical ingredient costs. Its two sheets are read by the Python loader, not reinterpreted from free-form model memory.

### Tools and MCP boundary

Hermes uses web search for recipe discovery and supplemental supplier quotes. The local FastMCP server named `sabor-da-maria` exposes 11 domain tools, including pantry inspection, kitchen profile management, recipe evaluation, explicit approval and approved-only calculation. The Python domain/application layers validate amounts, units, available equipment and skills, whole-batch production, remaining inventory, supplementary package purchases and the R$ 80 shared session budget. SQLite keeps the authoritative state transitions.

The read-only compatibility tools `calculate_recipe_cost` and `calculate_delivery_pricing` remain for historical contract/evaluation use; the **official stateful journey** requires `approve_recipe_candidate` before `calculate_approved_recipe`. This distinction is intentional and documented in the workflow section above.

### Memory and state management

- **Conversational memory:** Hermes's own conversation context is used to elicit answers and provide explanations. It is not treated as proof of an approved recipe or available stock.
- **Operational memory:** SQLite persists planning sessions, candidate versions, recipe/kitchen facts, evaluation snapshots, confirmation of batches, approval events and pricing results. `get_planning_status` retrieves the authoritative state even if the agent conversation changes.
- **Approval integrity:** explicit user words, approved candidate version and an input fingerprint are recorded; a changed kitchen, recipe, pantry or quote invalidates stale approval. Previous approved recipes reserve workbook inventory within their session. Different sessions are isolated.
- **Scope:** a transcript is not a replacement for authenticated user identity. Chosen selling-price scenarios are returned to the user but there is currently no official persistence field for the final commercial selection.

### Reproducing on a clean Windows machine

Prerequisites: Git, Python 3.12, `uv`, a separately installed Hermes Agent CLI, a configured provider/model for web search, and the challenge workbook. Do **not** commit API tokens, provider configuration or local SQLite databases.

```powershell
# From a fresh directory:
git clone https://github.com/FilipiWanderley/ifood-agent-challenge.git
Set-Location ifood-agent-challenge
# For reviewers of the proposed changes, check out the feature branch/PR first:
git checkout feat/recipe-domain-model
uv sync --locked --all-groups

# The original challenge workbook must exist at this exact location, or set
# SABOR_DA_MARIA_WORKBOOK to an absolute path to your copy of it.
Test-Path '.\data\raw\despensa_dona_maria.xlsx'

# Register the repository's installed console entry point with Hermes.
# Run once per Hermes user profile, not once per agent session.
$serverExe = (Resolve-Path '.\.venv\Scripts\sabor-da-maria-agent.exe').Path
hermes mcp add sabor-da-maria --command "$serverExe" --connect-timeout 60
hermes mcp test sabor-da-maria

uv run --locked ruff check src tests evals
uv run --locked ruff format --check src tests evals
uv run --locked mypy src
uv run --locked pyright
uv run --locked pytest tests/unit tests/integration tests/evaluation tests/contract -q
hermes
```

If the MCP name is **already configured**, inspect it with `hermes mcp list` and `hermes mcp test sabor-da-maria` rather than adding it again. The `.venv` path is local to each clone; reconfigure an existing Hermes MCP entry after moving the repository. Hermes/model installation and provider authentication are environment-level prerequisites, not bundled credentials.

The included `scripts/check-delivery.ps1` checks the same source tree and local MCP environment; it does not install Hermes or change configuration. Its initial workbook check reports whether the challenge data is available. CI's passing Python tests do not, by themselves, prove a new computer has Hermes configured or access to the workbook.
