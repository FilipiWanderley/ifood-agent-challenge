from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from sabor_da_maria_agent.domain.units import MeasureDimension
from sabor_da_maria_agent.infrastructure.excel.workbook_loader import (
    PantryWorkbookLoader,
    WorkbookSchemaError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_WORKBOOK = PROJECT_ROOT / "data" / "raw" / "despensa_dona_maria.xlsx"


def create_workbook(
    path: Path,
    stock_rows: list[tuple[object, ...]],
    purchase_rows: list[tuple[object, ...]],
) -> None:
    workbook = Workbook()

    stock_sheet = workbook.active
    stock_sheet.title = "Despensa"
    stock_sheet.append(
        ["Ingrediente", "Quantidade em estoque", "Unidade"]
    )

    for row in stock_rows:
        stock_sheet.append(list(row))

    purchase_sheet = workbook.create_sheet("Precos")
    purchase_sheet.append(
        [
            "Ingrediente",
            "Quantidade comprada",
            "Unidade",
            "Preço total pago (R$)",
        ]
    )

    for row in purchase_rows:
        purchase_sheet.append(list(row))

    workbook.save(path)
    workbook.close()


def test_loads_real_challenge_workbook() -> None:
    catalog = PantryWorkbookLoader().load(REAL_WORKBOOK)

    assert len(catalog.ingredients) == 37
    assert catalog.historical_purchase_total == Decimal("663.39")
    assert catalog.remaining_budget == Decimal("80.00")


def test_calculates_package_costs_using_base_units() -> None:
    catalog = PantryWorkbookLoader().load(REAL_WORKBOOK)

    ingredients = {
        ingredient.name: ingredient
        for ingredient in catalog.ingredients
    }

    alcaparras = ingredients["Alcaparras"]
    chantilly = ingredients["Chantilly"]
    azeite = ingredients["Azeite de oliva extra virgem"]

    assert alcaparras.purchase_base_quantity == Decimal(2000)
    assert alcaparras.unit_cost == Decimal("0.041")

    assert chantilly.purchase_base_quantity == Decimal(500)
    assert chantilly.unit_cost == Decimal("0.04734")

    assert azeite.purchase_base_quantity == Decimal(500)
    assert azeite.base_unit == "ml"
    assert azeite.unit_cost == Decimal("0.06198")


def test_preserves_unknown_package_weight_as_count() -> None:
    catalog = PantryWorkbookLoader().load(REAL_WORKBOOK)

    chocolate = next(
        ingredient
        for ingredient in catalog.ingredients
        if ingredient.name == "Cobertura de chocolate"
    )

    assert chocolate.base_unit == "un"
    assert chocolate.dimension == MeasureDimension.COUNT
    assert chocolate.purchase_base_quantity == Decimal(1)
    assert chocolate.unit_cost == Decimal("79.9")


def test_rejects_ingredients_without_matching_purchase(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing_purchase.xlsx"

    create_workbook(
        path,
        [("Arroz", 1, "kg")],
        [("Feijao", 1, "kg", 10)],
    )

    with pytest.raises(WorkbookSchemaError, match="reconciliation failed"):
        PantryWorkbookLoader().load(path)


def test_rejects_incompatible_measurement_dimensions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid_dimension.xlsx"

    create_workbook(
        path,
        [("Arroz", 1, "kg")],
        [("Arroz", 1, "L", 10)],
    )

    with pytest.raises(WorkbookSchemaError, match="dimension mismatch"):
        PantryWorkbookLoader().load(path)


def test_rejects_duplicate_ingredients(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate.xlsx"

    create_workbook(
        path,
        [
            ("Arroz", 1, "kg"),
            ("Arroz", 2, "kg"),
        ],
        [("Arroz", 1, "kg", 10)],
    )

    with pytest.raises(WorkbookSchemaError, match="Duplicate ingredient"):
        PantryWorkbookLoader().load(path)


def test_rejects_unsupported_units(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unsupported_unit.xlsx"

    create_workbook(
        path,
        [("Arroz", 1, "pacote")],
        [("Arroz", 1, "pacote", 10)],
    )

    with pytest.raises(WorkbookSchemaError, match="Unsupported unit"):
        PantryWorkbookLoader().load(path)


def test_rejects_non_positive_quantities(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid_quantity.xlsx"

    create_workbook(
        path,
        [("Arroz", 0, "kg")],
        [("Arroz", 1, "kg", 10)],
    )

    with pytest.raises(WorkbookSchemaError, match="must be positive"):
        PantryWorkbookLoader().load(path)