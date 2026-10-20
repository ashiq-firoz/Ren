"""Tiny stdio MCP server used by tests."""
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

server = MCPServer("echo-test")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    """Echo the text back in upper case."""
    return text.upper()


@server.tool()
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


if __name__ == "__main__":
    server.run("stdio")
