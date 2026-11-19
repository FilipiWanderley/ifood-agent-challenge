# Sabor da Maria Agent

An agentic AI application for intelligent recipe planning, pantry management, ingredient costing, and budget-aware shopping recommendations.

## Technology

- Python 3.12
- uv
- Pydantic
- OpenPyXL
- Pytest
- Ruff
- Mypy

## Setup

```powershell
uv sync --all-groups
```

## Quality Gates

```powershell
uv run pytest tests/unit tests/integration -v
uv run ruff check src tests
uv run mypy src
```

## Project Structure

- docs/: requirements, architecture and technical decisions
- data/: challenge datasets
- src/: application source code
- tests/: unit, integration and evaluation tests
- scripts/: development utilities

## Current Progress

- Pantry workbook ingestion
- Ingredient normalization
- Purchase cost reconciliation
- Input validation
- Automated tests