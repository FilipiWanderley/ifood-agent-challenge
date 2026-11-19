from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from unicodedata import normalize
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from sabor_da_maria_agent.domain.pantry import (
    IngredientCost,
    PantryCatalog,
)
from sabor_da_maria_agent.domain.units import normalize_measure


class WorkbookSchemaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StockRecord:
    name: str
    quantity: Decimal
    unit: str
    row_number: int


@dataclass(frozen=True, slots=True)
class PurchaseRecord:
    name: str
    quantity: Decimal
    unit: str
    total_paid: Decimal
    row_number: int


class PantryWorkbookLoader:
    STOCK_HEADERS = (
        "Ingrediente",
        "Quantidade em estoque",
        "Unidade",
    )

    PURCHASE_HEADERS = (
        "Ingrediente",
        "Quantidade comprada",
        "Unidade",
        "Preço total pago (R$)",
    )

    def __init__(
        self,
        remaining_budget: Decimal = Decimal("80.00"),
    ) -> None:
        if not remaining_budget.is_finite() or remaining_budget < 0:
            raise ValueError("Remaining budget must be a non-negative finite decimal.")

        self.remaining_budget = remaining_budget

    def load(self, path: Path) -> PantryCatalog:
        if not path.is_file():
            raise FileNotFoundError(f"Workbook not found: {path}")

        try:
            workbook = load_workbook(
                filename=path,
                read_only=True,
                data_only=True,
            )
        except (InvalidFileException, BadZipFile, OSError, ValueError) as exc:
            raise WorkbookSchemaError(
                f"Unable to read workbook: {path}"
            ) from exc

        try:
            required_sheets = {"Despensa", "Precos"}
            missing_sheets = required_sheets - set(workbook.sheetnames)

            if missing_sheets:
                raise WorkbookSchemaError(
                    f"Missing worksheets: {sorted(missing_sheets)}"
                )

            stock_records = self._read_stock_sheet(
                workbook["Despensa"]
            )
            purchase_records = self._read_purchase_sheet(
                workbook["Precos"]
            )

            if not stock_records:
                raise WorkbookSchemaError("The pantry worksheet is empty.")

            if not purchase_records:
                raise WorkbookSchemaError("The purchase worksheet is empty.")

            self._validate_reconciliation(
                stock_records,
                purchase_records,
            )

            ingredients = tuple(
                self._build_ingredient(
                    stock_records[key],
                    purchase_records[key],
                )
                for key in stock_records
            )

            historical_purchase_total = sum(
                (
                    record.total_paid
                    for record in purchase_records.values()
                ),
                start=Decimal(0),
            )

            return PantryCatalog(
                ingredients=ingredients,
                historical_purchase_total=historical_purchase_total,
                remaining_budget=self.remaining_budget,
            )
        finally:
            workbook.close()

    def _read_stock_sheet(
        self,
        sheet: Any,
    ) -> dict[str, StockRecord]:
        records: dict[str, StockRecord] = {}

        for row_number, values in self._data_rows(
            sheet,
            self.STOCK_HEADERS,
        ):
            name = self._required_text(
                values[0],
                "Despensa",
                row_number,
                "Ingrediente",
            )
            key = self._ingredient_key(name)

            if key in records:
                raise WorkbookSchemaError(
                    f"Duplicate ingredient {name!r} in Despensa, row {row_number}."
                )

            records[key] = StockRecord(
                name=name,
                quantity=self._to_decimal(
                    values[1],
                    "Despensa",
                    row_number,
                    "Quantidade em estoque",
                ),
                unit=self._required_text(
                    values[2],
                    "Despensa",
                    row_number,
                    "Unidade",
                ),
                row_number=row_number,
            )

        return records

    def _read_purchase_sheet(
        self,
        sheet: Any,
    ) -> dict[str, PurchaseRecord]:
        records: dict[str, PurchaseRecord] = {}

        for row_number, values in self._data_rows(
            sheet,
            self.PURCHASE_HEADERS,
        ):
            name = self._required_text(
                values[0],
                "Precos",
                row_number,
                "Ingrediente",
            )
            key = self._ingredient_key(name)

            if key in records:
                raise WorkbookSchemaError(
                    f"Duplicate ingredient {name!r} in Precos, row {row_number}."
                )

            records[key] = PurchaseRecord(
                name=name,
                quantity=self._to_decimal(
                    values[1],
                    "Precos",
                    row_number,
                    "Quantidade comprada",
                ),
                unit=self._required_text(
                    values[2],
                    "Precos",
                    row_number,
                    "Unidade",
                ),
                total_paid=self._to_decimal(
                    values[3],
                    "Precos",
                    row_number,
                    "Preço total pago",
                ),
                row_number=row_number,
            )

        return records

    def _data_rows(
        self,
        sheet: Any,
        expected_headers: tuple[str, ...],
    ) -> Iterator[tuple[int, tuple[Any, ...]]]:
        header = next(
            sheet.iter_rows(
                min_row=1,
                max_row=1,
                values_only=True,
            ),
            None,
        )

        if header is None:
            raise WorkbookSchemaError(
                f"Worksheet {sheet.title!r} has no header."
            )

        headers = tuple(
            self._normalize_header(value)
            for value in header
        )

        while headers and not headers[-1]:
            headers = headers[:-1]

        if headers != expected_headers:
            raise WorkbookSchemaError(
                f"Invalid headers in {sheet.title!r}. "
                f"Expected {expected_headers}, received {headers}."
            )

        for row_number, row in enumerate(
            sheet.iter_rows(
                min_row=2,
                values_only=True,
            ),
            start=2,
        ):
            if all(self._is_empty(value) for value in row):
                continue

            values = tuple(row)

            if len(values) < len(expected_headers):
                values += (None,) * (
                    len(expected_headers) - len(values)
                )

            if any(
                not self._is_empty(value)
                for value in values[len(expected_headers):]
            ):
                raise WorkbookSchemaError(
                    f"Unexpected columns in {sheet.title!r}, row {row_number}."
                )

            yield row_number, values[:len(expected_headers)]

    def _build_ingredient(
        self,
        stock: StockRecord,
        purchase: PurchaseRecord,
    ) -> IngredientCost:
        try:
            stock_measure = normalize_measure(
                stock.quantity,
                stock.unit,
            )
            purchase_measure = normalize_measure(
                purchase.quantity,
                purchase.unit,
            )
        except ValueError as exc:
            raise WorkbookSchemaError(
                f"Invalid measurement for ingredient {stock.name!r}: {exc}"
            ) from exc

        if stock_measure.dimension != purchase_measure.dimension:
            raise WorkbookSchemaError(
                f"Measurement dimension mismatch for {stock.name!r}: "
                f"stock={stock.unit!r}, purchase={purchase.unit!r}."
            )

        if stock_measure.unit != purchase_measure.unit:
            raise WorkbookSchemaError(
                f"Base unit mismatch for {stock.name!r}."
            )

        unit_cost = (
            purchase.total_paid / purchase_measure.quantity
        )

        return IngredientCost(
            name=stock.name,
            stock_quantity=stock.quantity,
            stock_unit=stock.unit,
            stock_base_quantity=stock_measure.quantity,
            purchase_quantity=purchase.quantity,
            purchase_unit=purchase.unit,
            purchase_base_quantity=purchase_measure.quantity,
            purchase_total=purchase.total_paid,
            base_unit=purchase_measure.unit,
            dimension=purchase_measure.dimension,
            unit_cost=unit_cost,
        )

    @staticmethod
    def _validate_reconciliation(
        stock_records: dict[str, StockRecord],
        purchase_records: dict[str, PurchaseRecord],
    ) -> None:
        stock_keys = set(stock_records)
        purchase_keys = set(purchase_records)

        missing_purchases = sorted(stock_keys - purchase_keys)
        missing_stock = sorted(purchase_keys - stock_keys)

        if missing_purchases or missing_stock:
            raise WorkbookSchemaError(
                "Ingredient reconciliation failed. "
                f"Missing in Precos: {missing_purchases}. "
                f"Missing in Despensa: {missing_stock}."
            )

    @staticmethod
    def _ingredient_key(value: str) -> str:
        return " ".join(
            normalize("NFKC", value).casefold().split()
        )

    @staticmethod
    def _normalize_header(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.strip().split())

    @staticmethod
    def _is_empty(value: Any) -> bool:
        return value is None or (
            isinstance(value, str) and not value.strip()
        )

    @staticmethod
    def _required_text(
        value: Any,
        sheet_name: str,
        row_number: int,
        field_name: str,
    ) -> str:
        if not isinstance(value, str) or not value.strip():
            raise WorkbookSchemaError(
                f"Invalid {field_name} in {sheet_name}, row {row_number}."
            )

        return value.strip()

    @staticmethod
    def _to_decimal(
        value: Any,
        sheet_name: str,
        row_number: int,
        field_name: str,
    ) -> Decimal:
        if isinstance(value, bool) or value is None:
            raise WorkbookSchemaError(
                f"Invalid {field_name} in {sheet_name}, row {row_number}."
            )

        raw_value = (
            value.strip().replace(",", ".")
            if isinstance(value, str)
            else str(value)
        )

        try:
            result = Decimal(raw_value)
        except InvalidOperation as exc:
            raise WorkbookSchemaError(
                f"Invalid numeric value for {field_name} "
                f"in {sheet_name}, row {row_number}."
            ) from exc

        if not result.is_finite() or result <= 0:
            raise WorkbookSchemaError(
                f"{field_name} must be positive and finite "
                f"in {sheet_name}, row {row_number}."
            )

        return result