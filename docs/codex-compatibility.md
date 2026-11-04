# Codex compatibility

The gateway is a model provider. The real Codex client owns shell execution,
patch application, MCP connections, skills, sandboxing, and approval decisions.
It never executes client tools on the server.

## Two explicit backends

| | `RESPONSES_BACKEND=web` (default) | `RESPONSES_BACKEND=native` |
|---|---|---|
| Upstream | ChatGPT web conversations | An explicitly configured authorized Responses endpoint |
| Model IDs | Actual web catalog IDs, e.g. `gpt-6-thinking` | Actual native catalog IDs, e.g. `gpt-6.1-terra` |
| Tool calls | Validated text bridge: function, namespace, custom text/grammar | Native Responses items, without text conversion |
| Output | Live final text without tools; tool-enabled turns buffered and validated | Upstream typed events and opaque reasoning state |
| Compaction | Gateway summary plus encrypted local checkpoint; no native reasoning preservation | Native endpoint or V2 compaction trigger; canonical output window |
| Limitations | Text only, no hosted tools or structured output; local context ceiling | Depends on configured provider and account capabilities |

Work-mode IDs ending in `-wm` are rejected by the web conversation route.
Model metadata never aliases Terra to a web model or grants unavailable tools.
Both backends keep `store=false` upstream. A native adapter is a separate route;
it does not turn a web subscription model into a native API model.

## Real CLI

Install Codex separately. Set `OPENAI_BASE_URL` to the gateway's complete
`http://127.0.0.1:5005/<API_PREFIX>/v1` URL, `OPENAI_API_KEY` to its **client key**,
and `CHATGPT_MODEL` to an available model ID. Keep credentials in local environment
variables; do not paste them into prompts or commit them.

```sh
python scripts/codex_run.py --ephemeral --cwd /absolute/path/to/project \
  --sandbox workspace-write 'Inspect the failing tests, fix the bug, then verify.'
```

The helper supplies provider configuration for this invocation. It does not edit
user configuration or install MCP servers. See [provider/MCP TOML](../examples/codex_gateway.toml).
Use the normal interactive Codex CLI for approval prompts and full session UX.
[The app-server example](../examples/codex_app_server.py) demonstrates
thread start/resume and follow-up turns; it declines execution/file approvals.
Use `--isolate-mcp` for a disposable probe that does not start configured MCP servers.

Codex model metadata is at `/v1/codex/models?client_version=0.166.0`, protected
by the gateway client key. Web metadata defaults to the qualified `0.166.0`
contract and a conservative local 32,768-token ceiling; this is not a claim
about the upstream model's maximum context. New CLI versions need qualification.

## Native operator setup

Choose an authorized Responses backend with its actual model catalog. Set
`RESPONSES_UPSTREAM_BASE_URL` and `RESPONSES_UPSTREAM_KEY` locally. The catalog
must use Codex Models format; ordinary OpenAI `/models` format is insufficient.
For native-only Responses, leave `CHATGPT_TOKEN_FILE` empty in `.env`; no web
account file is required. Keep a web credential file if also using Chat Completions.
For an operator's own Codex login, the optional override explicitly mounts its
existing login file read-only:

```sh
# Configure RESPONSES_BACKEND=native and the base URL in your private .env.
# CODEX_AUTH_SOURCE must be an absolute existing auth.json path.
docker compose -f compose.yaml -f compose.codex.yaml up -d --build
```

For current Codex subscription transport, explicitly set
`RESPONSES_COMPACTION_MODE=responses` to request native V2 compaction inside
Responses. The default `endpoint` mode uses `/responses/compact`. A failure
never triggers an automatic second request through another route.

This path does not refresh or modify the login file. Expired credentials remain
an authentication error. Web accessToken and Codex login are separate credentials.
A shared native credential serves requests under that operator account; bind to
loopback and share gateway client keys only with intended callers.

## State, retries, transport

- HTTP SSE emits created/in-progress, output items/deltas, and a terminal
  completed/incomplete/failed state. A truncated tool envelope fails validation.
  Native events retain their types. Web usage numbers are explicitly estimates.
- `previous_response_id` is a gateway-owned full-context replay, including on
  WebSocket. Default state is in RAM for 900 seconds, at most 128 records and
  8 MiB per record. Expired/evicted state returns an error, never a new empty turn.
- `store=false` writes no response checkpoint to disk and disallows retrieval.
  `store=true` enables local GET/DELETE; disk persistence additionally requires
  `RESPONSES_STATE_DIR` and a stable private `RESPONSES_STATE_SECRET`.
  Disk checkpoints are AES-GCM encrypted and owner-scoped. Expired files are
  ignored; operators should remove old checkpoint files according to retention needs.
- `Idempotency-Key` replays a completed result within the same bounded in-memory
  window. Pending/unknown outcomes return 409; it is not durable exactly-once
  execution. No inference retries or automatic text continuation occur in Responses.
- Optional `RESPONSES_WEBSOCKET_ENABLED=true` provides Bearer-authenticated,
  **one-response-per-socket-at-a-time** `response.create`/`response.cancel`.
  It does not implement multiplexed `stream_id` or native upstream WS caching.
  `generate:false` warms local request state without calling upstream inference.
- Deadlines, bounded queues, cancellation, and redacted lifecycle metadata apply.
  Cancelling local work does not prove that remote generation has stopped.

## MCP and acceptance

stdio and Streamable HTTP MCP servers connect to Codex, which sends their tools
and results through Responses. For supported OAuth servers, use
`codex mcp login <server>`. The gateway does not receive MCP OAuth credentials.

Opt-in acceptance spends actual model usage, uses an isolated temporary workspace,
and a disposable client home, disables unrelated plugins, removes inherited Desktop
session/tool-pipe variables, and writes only a sanitized summary. This invocation
uses High reasoning and never edits user configuration. Choose a unique output per run; the exclusive
`.started` marker prevents an accidental replay after an unknown timeout.

```sh
python scripts/codex_acceptance.py --case hello --output diagnostics/hello.json
python scripts/codex_acceptance.py --case coding --output diagnostics/coding.json
python scripts/codex_acceptance.py --case mcp \
  --mcp-python /absolute/path/to/python-with-example-dependencies \
  --output diagnostics/mcp.json
```

For the additional web checks, use a quiet, isolated local web instance and its
host-side lifecycle audit file. Set `RESPONSES_WEBSOCKET_ENABLED=true` on that
instance before the WebSocket check. This suite verifies the actual backend and
model from server audit entries; WebSocket acceptance rejects HTTP fallback and
requires every observed warmup to have zero generation POSTs.

```sh
python scripts/web_qualification.py --case http-mcp \
  --mcp-python /absolute/path/to/python-with-example-dependencies \
  --audit-file data/lifecycle-audit.jsonl --output diagnostics/web-http-mcp.json
python scripts/web_qualification.py --case app-server \
  --audit-file data/lifecycle-audit.jsonl --output diagnostics/web-app-server.json
python scripts/web_qualification.py --case previous-response \
  --audit-file data/lifecycle-audit.jsonl --output diagnostics/web-previous.json
python scripts/web_qualification.py --case websocket \
  --audit-file data/lifecycle-audit.jsonl --output diagnostics/web-websocket.json
```

Check each result before starting the next case. Use a fresh output name for a
new intentional run. Failed/unknown runs are not automatically replayed. The
app-server fixture uses one ephemeral thread and two turns; response-ID testing
uses gateway RAM replay with `store=false`, not native hidden-reasoning state.

See [validation evidence](validation.md) for exactly which real routes passed.
Offline protocol success alone does not establish live model availability.

## Upgrade and rollback

Keep private `.env`, `data/`, native login mounts and the previous image digest.
Build the new image before replacing the gateway container; test the chosen model
route with the opt-in acceptance script. No global Codex configuration is migrated.
Roll back by running the previous image with the preserved private configuration.
RAM response IDs disappear on restart. Persistent checkpoints require the same
state secret and a compatible gateway version; an older release without Responses
cannot resume them. Do not delete account data or checkpoints during rollback.
