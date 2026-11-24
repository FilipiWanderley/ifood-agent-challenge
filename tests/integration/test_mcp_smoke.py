import asyncio
from decimal import Decimal

from fastmcp import Client

from sabor_da_maria_agent.infrastructure.mcp.server import mcp


async def main():
    async with Client(mcp) as client:
        tools = await client.list_tools()

        print("FERRAMENTAS REGISTRADAS:")
        for tool in tools:
            print(f"- {tool.name}")

        expected = {
            "get_pantry_inventory",
            "get_pantry_summary",
            "calculate_recipe_cost",
            "calculate_delivery_pricing",
        }

        actual = {tool.name for tool in tools}

        assert expected.issubset(actual), f"Missing tools: {expected - actual}"

        summary_result = await client.call_tool("get_pantry_summary", {})

        assert not summary_result.is_error
        summary = summary_result.data

        assert summary["ingredient_count"] == 37

        print("\nRESUMO DA DESPENSA:")
        print(summary)

        inventory_result = await client.call_tool("get_pantry_inventory", {})

        assert not inventory_result.is_error
        inventory = inventory_result.data

        ingredient = inventory["ingredients"][0]

        stock_quantity = Decimal(ingredient["stock_base_quantity"])

        required_quantity = min(
            stock_quantity,
            Decimal(1),
        )

        arguments = {
            "recipe_name": "Teste de integracao MCP",
            "requirements": [
                {
                    "name": ingredient["name"],
                    "quantity": str(required_quantity),
                    "unit": ingredient["base_unit"],
                }
            ],
            "servings": 1,
            "supplemental_quotes": [],
        }

        pricing_result = await client.call_tool(
            "calculate_delivery_pricing",
            arguments,
        )

        assert not pricing_result.is_error

        pricing_data = pricing_result.data

        assert len(pricing_data["pricing_analysis"]["scenarios"]) == 3

        print("\nCMV:")
        print(pricing_data["recipe_cost"]["total_cmv"])

        print("\nPRECO DE EQUILIBRIO:")
        print(pricing_data["break_even_price_brl"])

        print("\nCENARIOS DE PRECIFICACAO:")
        for scenario in pricing_data["pricing_analysis"]["scenarios"]:
            print(
                scenario["target_margin_on_gross_price"],
                scenario["selling_price"],
                scenario["estimated_profit"],
            )

        print("\nMCP INTEGRATION TEST PASSED")


def test_mcp_server_integration() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
