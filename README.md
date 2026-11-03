# ChatGPT Web Gateway

[English](README.md) | [简体中文](README.zh-CN.md)

Turn a ChatGPT web session into an **OpenAI-compatible Chat Completions and Codex Responses endpoints**, with **function calling, client-side MCP tools, and streaming responses**.

Use the standard OpenAI SDK workflow: declare `tools`, receive `tool_calls`, execute them in your client, and send results back as `role: "tool"`. Existing clients that implement this Chat Completions tool loop can use the gateway by changing their base URL and API key, within the compatibility scope below.

Derived from [lanqian528/chat2api](https://github.com/lanqian528/chat2api), with its MIT license retained. This is an independent community project, not an official OpenAI API service.

## Features

- OpenAI-compatible `POST /v1/chat/completions` and `GET /v1/models`.
- **Codex integration:** Responses SSE, function/custom/namespace tools, client-side MCP, bounded replay and compaction; explicit web and native backends. [Setup and limits](docs/codex-compatibility.md).
- **Standard function tools:** `tools`, `tool_choice`, `parallel_tool_calls`, assistant `tool_calls`, matching `tool_call_id` results, and subsequent model turns.
- **MCP integration:** your client connects to MCP servers, converts their tool schemas into function tools, executes requested MCP calls, and returns results to the model. A complete stdio example is included.
- SSE response framing, including tool-call deltas, optional usage chunks, and `[DONE]`.
- Account-specific model discovery, explicit reasoning-effort selection, and rejection of unknown model names instead of silent fallback.
- Bounded continuation for long responses, WebSocket/SSE recovery, request deadlines, and cancellation on client disconnect.
- Request IDs and bounded diagnostic metadata, including recognized upstream error codes such as `usage_limit`.

## Quick start

Requires Docker with Compose and Python 3 for generating the local configuration.

```bash
git clone https://github.com/pythc/chatgpt-web-gateway.git
cd chatgpt-web-gateway
python3 scripts/configure.py
docker compose up -d --build
```

The configuration generator creates a random client API key and URL prefix in a local `.env` file with mode `0600`. It does not overwrite an existing configuration or print credentials.

1. Read `API_PREFIX` from your local `.env` and open `http://127.0.0.1:5005/<API_PREFIX>/tokens`.
2. Paste your own ChatGPT access token, or session JSON containing `accessToken`, into the password field and save it locally.
3. Configure your API client:

| Setting | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:5005/<API_PREFIX>/v1` |
| API key | `AUTHORIZATION` from your `.env` |
| Model | An exact model ID returned by `/v1/models` |

The client API key and ChatGPT login token are different credentials. Keep the login token on the gateway. `.env`, `data/`, logs, and local diagnostic artifacts are excluded from Git and Docker build contexts.

Compose publishes only to `127.0.0.1`. The inherited account-management routes use the URL prefix and do not have separate administrator authentication; keep them local.

If your network requires a proxy, set `PROXY_URL` in `.env`. From Docker Desktop, a host proxy can be reached using an address such as `http://host.docker.internal:7897`; replace the port with your own. No proxy is configured by default.

## Standard OpenAI SDK usage

Install the optional example dependencies in your preferred virtual environment:

```bash
pip install -r requirements.examples.txt
export OPENAI_BASE_URL='http://127.0.0.1:5005/<API_PREFIX>/v1'
export OPENAI_API_KEY='<AUTHORIZATION from your local .env>'
export CHATGPT_MODEL='<model ID from /v1/models>'
python examples/chat.py
```

The request format is the familiar Chat Completions format:

```python
from openai import OpenAI

client = OpenAI()  # Reads OPENAI_BASE_URL and OPENAI_API_KEY.
response = client.chat.completions.create(
    model="<model ID from /v1/models>",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=1024,
)
print(response.choices[0].message.content)
```

The included examples disable SDK retries to avoid replaying a generation whose outcome is unknown.

## Function calling: the normal client tool loop

```bash
python examples/tools.py
```

The example declares an `add` function with a JSON Schema and completes this loop:

```text
client sends tools + messages
  → model requests a tool
  → gateway returns assistant.tool_calls (finish_reason="tool_calls")
  → client executes the declared function
  → client appends role="tool" with the matching tool_call_id
  → model returns its final answer
```

Supported controls include `tool_choice="auto"`, `"none"`, `"required"`, a named function choice, and `parallel_tool_calls`. Tool arguments are validated against the declared JSON Schemas. The gateway only produces validated calls; **your client executes the tools**.

Internally, this is a text-protocol bridge over the web model, rather than native API function calling. Invalid or incomplete envelopes fail explicitly instead of being executed as partial calls. The OpenAI-compatible client protocol does not imply identical model reliability or latency to the official API.

## Codex CLI and Responses

Use the real Codex CLI with the gateway as its model provider. Codex retains its tools, MCP connections, sandbox and approvals. See [setup, backend differences and acceptance commands](docs/codex-compatibility.md), [provider/MCP configuration](examples/codex_gateway.toml), and [app-server example](examples/codex_app_server.py).

Web `gpt-6-thinking` and native `gpt-6.1-terra` are distinct routes. Compatibility does not mean every native API capability exists on the web backend.

Real CLI coding is qualified on native Terra. Web function/MCP calls passed, but web automatic shell/patch coding has **not passed**; see [acceptance evidence](docs/validation.md).

## MCP tools

```bash
python examples/mcp_client.py
```

This starts a harmless local MCP server and demonstrates:

```text
MCP tools/list → OpenAI tools → model tool_calls
              → MCP tools/call → role="tool" → final answer
```

The server returns a random value, so the answer must come from an actual MCP execution. See [the client](examples/mcp_client.py) and [the server](examples/mcp_server.py).

MCP belongs to the client layer in this project. The gateway accepts the resulting function definitions and tool results; it does not manage MCP server connections. Other MCP transports can be handled by your client without changing the gateway's Chat Completions format. The bundled example uses MCP Python SDK v2 (`mcp==2.3.0`).

## Streaming and compatibility

For **Chat Completions**, set `stream=True` as usual. Tool mode and automatic continuation **buffer the model output before emitting SSE**, so they are not token-by-token live upstream streams. This allows complete tool-envelope validation and safe response assembly. Tool deltas, finish reasons, and `[DONE]` use the Chat Completions shape.

| Capability | Support / scope |
| --- | --- |
| Chat Completions text and message history | Supported |
| Function `tools` and tool-result round trips | Supported; at most 128 tools, `n=1` |
| Client-side MCP tools | Supported through the function-tool loop |
| SSE and `stream_options.include_usage` | SSE supported; usage chunks in buffered tool/continuation mode |
| `response_format: {"type":"json_object"}` | Supported for text requests; cannot be combined with tool mode |
| `max_tokens`, `max_completion_tokens` | Local bounds in tool/continuation mode; use `max_tokens` for direct plain text; do not raise upstream limits |
| `reasoning_effort`, `thinking_effort` | Account/model dependent; see configuration below |
| Legacy `functions` / `function_call` | Rejected; use `tools` / `tool_choice` |
| Remote JSON Schema references | Rejected; local references only |
| Responses API / Codex CLI | Supported within the [documented backend scope](docs/codex-compatibility.md) |
| Hosted MCP / native API hosted web search or code execution on the web backend | Unsupported; use client tools |

Standard API clients can use the supported flows normally. This is **not a claim of full parity with every OpenAI API endpoint or built-in tool**.

## Models, output limits, and timeouts

The sample configuration selects `gpt-6-thinking` with `high` effort. This is a configurable example, not a guarantee that your account currently exposes that model. Query `/v1/models` and select an available ID; explicit unknown models fail rather than falling back.

`reasoning_effort` uses a local compatibility mapping: `low → min`, `medium → standard`, `high → extended`, `xhigh → max`. You can also supply the web `thinking_effort` value using the SDK's `extra_body`. Values must appear in the selected model's account-specific catalog; conflicting values are rejected. An explicit different model does not automatically inherit the default model's effort.

| Environment variable | Sample value | Meaning |
| --- | --- | --- |
| `DEFAULT_MODEL` | `gpt-6-thinking` | Default requested model |
| `DEFAULT_REASONING_EFFORT` | `high` | Effort for the default model |
| `HISTORY_DISABLED` | `true` | Temporary web conversations |
| `AUTO_CONTINUE` | `true` | Assemble bounded continuations; per-request `auto_continue=false` disables this |
| `AUTO_CONTINUE_MAX_ROUNDS` | `4` | Additional model turns allowed for continuation |
| `AUTO_CONTINUE_MAX_TOKENS` | `65536` | Local assembled-output token cap |
| `DEFAULT_TOOL_MAX_TOKENS` | `65536` | Default local tool-mode output cap |
| `AUTO_CONTINUE_TIMEOUT` | `600` | Overall request work budget, including direct mode |
| `CONVERSATION_TIMEOUT` | `180` | Conversation transport timeout |
| `WEBSOCKET_IDLE_TIMEOUT` | `15` | Idle recovery wait |
| `RECOVERY_MAX_SECONDS` | `120` | Recovery time budget |
| `RECOVERY_MAX_READS` | `120` | Recovery read cap |

These values come from `.env.example`; source fallbacks may differ. Continuation can make additional model requests and consume additional usage. It cannot bypass an upstream output cap or usage restriction, and it may return a partial answer with `finish_reason="length"` when a bound is reached.

## Diagnostics and validation

Send a UUID in `X-Client-Request-Id` to correlate it with the returned `X-Request-Id`. Request metadata is stored in `data/lifecycle-audit.jsonl` and `data/lifecycle-activity.jsonl`. Do not publish the runtime `data/` directory.

An HTTP 200 from `/v1/models` proves model discovery, not generation access. An upstream SSE stream can start with HTTP 200 and later report a logical error. A recognized `usage_limit` is retained in `trace.lifecycle.upstream_error.code`; it does not identify the allocation category or reset time. Raising timeouts or switching effort is not evidence of a fix.

Offline regression tests use synthetic data and do not make ChatGPT requests:

```bash
docker build -t chatgpt-web-gateway:test .
docker run --rm --network none --entrypoint python \
  chatgpt-web-gateway:test scripts/test.py
```

Or, with Python 3.11 and the runtime dependencies installed:

```bash
pip install -r requirements.txt
python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
python scripts/test.py
```

The tokenizer is cached during the image build so the container test run can stay offline. See [validation scope](docs/validation.md) and [architecture](docs/architecture.md). Web protocol changes, account availability, and model behavior can still affect live requests; offline tests do not establish current upstream availability.

## Project layout

```text
api/          OpenAI-compatible routes, tool bridge, model catalog, continuation
chatgpt/      Web transport, response state machine, recovery, request ownership
utils/        Configuration, HTTP client, tokenization support
examples/     Standard SDK, function tools, and client-side MCP examples
tests/        Synthetic offline regression suites and fixtures
scripts/      Local setup and portable test runner
```

The inherited optional website-mirror routes remain disabled by the sample configuration. Existing local deployments are separate from this source distribution.

## License

[MIT](LICENSE). See [NOTICE](NOTICE) for upstream attribution.
