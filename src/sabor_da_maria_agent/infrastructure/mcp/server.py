import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from sabor_da_maria_agent.domain.costing import (
    DeliveryPricingCalculator,
    RecipeCost,
    RecipeCostCalculator,
    RecipeIngredientRequirement,
    SupplementalPurchaseQuote,
)
from sabor_da_maria_agent.domain.pantry import PantryCatalog
from sabor_da_maria_agent.infrastructure.excel.workbook_loader import (
    PantryWorkbookLoader,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_WORKBOOK = (
    PROJECT_ROOT / "data" / "raw" / "despensa_dona_maria.xlsx"
)


def _workbook_path() -> Path:
    configured = os.getenv("SABOR_DA_MARIA_WORKBOOK")

    if not configured:
        return DEFAULT_WORKBOOK

    path = Path(configured).expanduser()

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path


def _load_catalog() -> PantryCatalog:
    return PantryWorkbookLoader().load(_workbook_path())


def _parse_requirements(
    items: list[dict[str, str]],
) -> list[RecipeIngredientRequirement]:
    if not items:
        raise ValueError("At least one recipe ingredient is required.")

    parsed = []

    for index, item in enumerate(items, start=1):
        required = {"name", "quantity", "unit"}
        missing = required.difference(item)
        extra = set(item).difference(required)

        if missing or extra:
            raise ValueError(
                f"Invalid requirement at position {index}. "
                f"Missing: {sorted(missing)}. "
                f"Unexpected: {sorted(extra)}."
            )

        parsed.append(
            RecipeIngredientRequirement(
                name=item["name"],
                quantity=Decimal(item["quantity"]),
                unit=item["unit"],
            )
        )

    return parsed


def _parse_quotes(
    items: list[dict[str, str]] | None,
) -> list[SupplementalPurchaseQuote]:
    if not items:
        return []

    parsed = []

    for index, item in enumerate(items, start=1):
        required = {
            "name",
            "package_quantity",
            "package_unit",
            "package_price",
            "source",
        }

        missing = required.difference(item)
        extra = set(item).difference(required)

        if missing or extra:
            raise ValueError(
                f"Invalid supplemental quote at position {index}. "
                f"Missing: {sorted(missing)}. "
                f"Unexpected: {sorted(extra)}."
            )

        parsed.append(
            SupplementalPurchaseQuote(
                name=item["name"],
                package_quantity=Decimal(item["package_quantity"]),
                package_unit=item["package_unit"],
                package_price=Decimal(item["package_price"]),
                source=item["source"],
            )
        )

    return parsed


def _calculate_recipe(
    recipe_name: str,
    requirements: list[dict[str, str]],
    servings: int,
    supplemental_quotes: list[dict[str, str]] | None,
) -> RecipeCost:
    return RecipeCostCalculator().calculate(
        recipe_name=recipe_name,
        requirements=_parse_requirements(requirements),
        catalog=_load_catalog(),
        supplemental_quotes=_parse_quotes(supplemental_quotes),
        servings=servings,
    )


mcp = FastMCP(
    name="Dona Maria - Consultora de Cardapio",
    instructions=(
        "Servidor de dominio para consulta de despensa, "
        "calculo deterministico de CMV e precificacao de delivery. "
        "Utilize as ferramentas para obter resultados reais. "
        "Nao invente estoque, precos ou cotacoes."
    ),
)


@mcp.tool
def get_pantry_inventory() -> dict[str, Any]:
    """Retorna todos os ingredientes e quantidades da despensa real."""

    catalog = _load_catalog()

    ingredients = [
        {
            "name": item.name,
            "stock_quantity": str(item.stock_quantity),
            "stock_unit": item.stock_unit,
            "stock_base_quantity": str(item.stock_base_quantity),
            "base_unit": item.base_unit,
            "dimension": item.dimension.value,
            "purchase_quantity": str(item.purchase_quantity),
            "purchase_unit": item.purchase_unit,
            "purchase_base_quantity": str(item.purchase_base_quantity),
            "purchase_total_brl": str(item.purchase_total),
            "unit_cost_brl": str(item.unit_cost),
        }
        for item in catalog.ingredients
    ]

    return {
        "workbook": _workbook_path().name,
        "ingredient_count": len(ingredients),
        "historical_purchase_total_brl": str(
            catalog.historical_purchase_total
        ),
        "remaining_budget_brl": str(catalog.remaining_budget),
        "ingredients": ingredients,
    }


@mcp.tool
def get_pantry_summary() -> dict[str, Any]:
    """Retorna resumo financeiro e quantidade de ingredientes."""

    catalog = _load_catalog()

    return {
        "ingredient_count": len(catalog.ingredients),
        "historical_purchase_total_brl": str(
            catalog.historical_purchase_total
        ),
        "remaining_budget_brl": str(catalog.remaining_budget),
        "workbook": _workbook_path().name,
    }


@mcp.tool
def calculate_recipe_cost(
    recipe_name: str,
    requirements: list[dict[str, str]],
    servings: int = 1,
    supplemental_quotes: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Calcula CMV usando estoque real, unidades normalizadas
    e cotacoes verificaveis para compras complementares.

    requirements:
      [{"name": "Farinha", "quantity": "200", "unit": "g"}]

    supplemental_quotes:
      [{
        "name": "Farinha",
        "package_quantity": "1",
        "package_unit": "un 500g",
        "package_price": "20.00",
        "source": "https://fornecedor.example/produto"
      }]
    """

    result = _calculate_recipe(
        recipe_name,
        requirements,
        servings,
        supplemental_quotes,
    )

    return {
        "recipe_cost": result.model_dump(mode="json"),
        "total_cmv_brl": str(result.total_cmv),
        "cmv_per_portion_brl": str(result.cmv_per_portion),
        "supplemental_purchase_total_brl": str(
            result.supplemental_purchase_total
        ),
        "budget_limit_brl": str(result.budget_limit),
        "budget_remaining_after_purchase_brl": str(
            result.budget_remaining_after_purchase
        ),
        "within_budget": result.within_budget,
    }


@mcp.tool
def calculate_delivery_pricing(
    recipe_name: str,
    requirements: list[dict[str, str]],
    servings: int = 1,
    supplemental_quotes: list[dict[str, str]] | None = None,
    include_batch_pricing: bool = False,
    pending_cost_items: list[str] | None = None,
) -> dict[str, Any]:
    """
    Calcula CMV e precificacao de delivery com taxa de 10 por cento,
    com base de custo explicita.

    Parametros adicionais (opcionais, compativeis com chamadas antigas):
      include_batch_pricing: true para incluir os cenarios do lote
        completo (CMV total). Padrao: false.
      pending_cost_items: nomes de ingredientes usados mas ainda sem
        quantidade definida (ex.: ["Sal"]). Nunca entram no CMV; quando
        informados, cmv_complete = false e todos os precos sao parciais.
        Nao podem repetir ingredientes de requirements.

    Retorno principal:
      servings, total_cmv_brl, cmv_per_portion_brl,
      break_even_price_per_portion_brl, cmv_complete, pending_cost_items
      delivery_pricing.per_portion: preco de cardapio (1 porcao), com
        cost_basis = CMV por porcao e cenarios de 15, 25 e 35 por cento
      delivery_pricing.batch: cenarios do lote (CMV total) ou null

    Campos legados (mantidos por compatibilidade, base = CMV total da
    receita, ou seja, preco do lote quando servings > 1):
      pricing_analysis, break_even_price_brl
    """

    recipe_cost = _calculate_recipe(
        recipe_name,
        requirements,
        servings,
        supplemental_quotes,
    )

    calculator = DeliveryPricingCalculator()
    report = calculator.calculate(
        recipe_cost,
        include_batch=include_batch_pricing,
        pending_cost_items=pending_cost_items or (),
    )
    legacy_pricing = (
        report.batch
        if report.batch is not None
        else calculator.policy.evaluate(recipe_cost)
    )

    return {
        "recipe_cost": recipe_cost.model_dump(mode="json"),
        "servings": report.servings,
        "total_cmv_brl": str(report.total_cmv),
        "cmv_per_portion_brl": str(report.cmv_per_portion),
        "break_even_price_per_portion_brl": str(
            report.per_portion.break_even_price
        ),
        "cmv_complete": report.cmv_complete,
        "pending_cost_items": list(report.pending_cost_items),
        "delivery_pricing": report.model_dump(mode="json"),
        "pricing_analysis": legacy_pricing.model_dump(mode="json"),
        "platform_fee_rate": str(report.platform_fee_rate),
        "break_even_price_brl": str(legacy_pricing.break_even_price),
        "currency": "BRL",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
