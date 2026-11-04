# Compatibility and validation

The gateway is tested as a model provider. Clients retain tool execution, MCP
connections, sandboxing and approvals.

## Qualified configuration

- Codex CLI: `0.166.0-alpha.5.1` (catalog contract `0.166.0`).
- Web model: `gpt-6-thinking`, High effort.
- Native model: `gpt-6.1-terra`, on an explicitly configured native backend.
- Regression suite: 288 tests across 18 suites; run without Docker networking.

These are tested configurations, not a promise of current model availability for
another account or support for every future Codex version.

## Tested flows

| Flow | Web | Native |
| --- | --- | --- |
| Real CLI response | Passed | Passed |
| Shell, patch and original-test repair | Passed | Passed |
| Client-side stdio MCP | Passed | Passed |
| Client-side Streamable HTTP MCP | Passed | Passed |
| app-server thread/start and follow-up | Passed | Passed |
| `previous_response_id` continuation | Passed; gateway replay | Passed; gateway replay |
| Real CLI WebSocket | Passed; no HTTP fallback | Passed |
| Compaction and continuation | Offline protocol checks; gateway summary | Passed; native opaque compaction |

Web coding checks include a filename containing a space and Chinese characters.
Acceptance requires actual Shell and patch items, successful client-issued tests,
unchanged original tests, and a completed CLI turn. MCP checks require a real server
lookup and an exact randomized result. Context checks withhold the remembered fact
from the follow-up request. WebSocket checks verify server transport records and
zero-generation warmups.

The file-based setup also has credential parsing, read-only file, malformed-input
and startup checks. The old account-management HTTP routes return 404.

## Run offline checks

```sh
docker build -t chatgpt-web-gateway:test .
docker run --rm --network none --entrypoint python \
  chatgpt-web-gateway:test scripts/test.py
```

Or use Python 3.11 with the runtime dependencies:

```sh
pip install -r requirements.txt
python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
python scripts/test.py
```

The runner isolates account state, dotenv and inherited Responses credentials.
The image caches the tokenizer so the container suites can run offline. CI runs
only synthetic checks and never starts an actual model generation.

## Run live checks

Live acceptance is opt-in and consumes model usage. Configure your client
credentials, choose a qualified model, and use a unique output path per run:

```sh
python scripts/codex_acceptance.py --case coding --output diagnostics/coding.json
python scripts/codex_acceptance.py --case mcp \
  --mcp-python /absolute/path/to/python-with-example-dependencies \
  --output diagnostics/mcp.json
```

For HTTP MCP, app-server, response-ID and WebSocket checks, use
[`scripts/web_qualification.py`](../scripts/web_qualification.py) against an
isolated instance and its audit file. Complete commands and backend limits are in
[Codex compatibility](codex-compatibility.md#mcp-and-acceptance).

A passing run verifies that flow on the selected account and version. Upstream
network, authentication, quotas and model protocol failures can still interrupt
future requests. Incomplete or invalid tool frames fail validation. Unknown model
outcomes are never automatically retried, and local cancellation does not establish
that remote generation stopped. The web adapter does not preserve native hidden
reasoning or implement every official API endpoint and hosted tool.
