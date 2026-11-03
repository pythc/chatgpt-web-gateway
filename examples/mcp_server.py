"""A harmless stdio MCP server for the client-side round-trip example."""
import secrets
from mcp.server import MCPServer

server = MCPServer('Web gateway example', log_level='WARNING')
MARKER = 'MCP_RESULT_' + secrets.token_hex(16)


@server.tool()
def fixture_lookup(key: str) -> dict[str, str]:
    """Look up a random value for key mcp_sample; never invent this value."""
    if key != 'mcp_sample':
        raise ValueError('Unknown example key')
    return {'key': key, 'value': MARKER}


if __name__ == '__main__':
    server.run(transport='stdio')
