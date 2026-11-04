"""Opt-in live Codex acceptance fixture: random read-only lookup with receipts."""
import json
import os
from pathlib import Path
import secrets
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer('Gateway Codex acceptance', log_level='WARNING')
marker = 'MCP_VERIFIED_' + secrets.token_hex(16)
receipts = Path(os.environ['MCP_FIXTURE_RECEIPTS'])
executions = 0


@server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True))
def fixture_lookup(key: str) -> dict[str, str]:
    """Read the actual random fixture value for mcp_sample. Do not invent it."""
    global executions
    if key != 'mcp_sample':raise ValueError('Unknown fixture key')
    executions += 1
    result = {'key': key, 'value': marker, 'execution_count': str(executions)}
    fd = os.open(receipts, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as stream:stream.write(json.dumps(result) + '\n')
    return result


if __name__ == '__main__':
    transport = os.getenv('MCP_FIXTURE_TRANSPORT', 'stdio')
    if transport == 'streamable-http':
        server.run(transport=transport, host='127.0.0.1', port=int(os.environ['MCP_FIXTURE_PORT']))
    elif transport == 'stdio':
        server.run(transport=transport)
    else:
        raise ValueError('Unsupported fixture transport')
