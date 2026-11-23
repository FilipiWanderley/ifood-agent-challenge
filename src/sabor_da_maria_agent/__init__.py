def main() -> None:
    """Start the Sabor da Maria MCP server over stdio."""
    from sabor_da_maria_agent.infrastructure.mcp.server import mcp

    mcp.run(transport="stdio")