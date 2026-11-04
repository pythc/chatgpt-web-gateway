# Configuration

ChatGPT Web Gateway runs as an API service. Configure it using `.env` and a
local credential file, then start Docker Compose. The default documentation and
operator messages are English. No browser-based account administration is included.

## Files and settings

| File / setting | Purpose |
| --- | --- |
| `.env` | Gateway client key, URL prefix, backend, model, reasoning effort and proxy |
| `AUTHORIZATION` | Client-facing API key; choose a random private value |
| `API_PREFIX` | URL routing prefix, e.g. `gateway`; it is not administrator authentication |
| `CHATGPT_TOKEN_FILE` | Credential path inside the container; example: `data/token.txt` |
| `data/token.txt` | Operator-managed ChatGPT credentials, loaded at startup |
| `data/` | Private runtime state and diagnostics; keep it across container upgrades |

The normal Compose mount maps `./data` to `/app/data`. Therefore `data/token.txt`
inside the container refers to `./data/token.txt` on the host. `.env` and runtime
files are ignored by Git and excluded from the image build context. If you keep
credentials elsewhere, use a location outside the checkout/build context.
The single-service Compose setup uses Docker's built-in bridge, without allocating
a separate project network. Host port publishing remains bound to loopback.

Set the example's placeholder client key and prefix before starting. The optional
`scripts/configure.py` helper generates private values without printing them and
refuses to overwrite an existing `.env`. Python is optional for deployment.

## Credential formats

The file accepts one of these complete formats:

- Plain tokens, one per line. Blank lines and lines starting with `#` are ignored.
- A complete ChatGPT session JSON object containing the string `accessToken`.
- A JSON array of token strings for multiple accounts.

Values are normalized and deduplicated in memory. Session metadata is not used as
a credential. The source file is never rewritten, and malformed JSON or invalid
token entries fail startup without including their contents in the error message.
An explicitly configured missing file also fails startup. This is syntax/configuration
validation; actual account access still requires a successful upstream request.

Existing installations that omit `CHATGPT_TOKEN_FILE` continue to read
`data/token.txt`. Keep `.env` and credential files private, for example mode `0600`,
and the containing directory `0700`. Update them using your text editor or secret
manager; avoid putting tokens in command arguments, URLs or Git.

For a read-only secret mount, set `CHATGPT_TOKEN_FILE=/run/secrets/chatgpt-token`
and add an operator-managed Compose override:

```yaml
services:
  gateway:
    volumes:
      - type: bind
        source: /absolute/private/path/chatgpt-session.json
        target: /run/secrets/chatgpt-token
        read_only: true
        bind:
          create_host_path: false
```

Native-only Responses deployments can leave `CHATGPT_TOKEN_FILE` unset or empty
and use the explicit native configuration in [Codex compatibility](codex-compatibility.md).
Chat Completions still use the web backend and need web credentials when used.

## Reloading and upgrading

After changing credential contents, restart the service:

```bash
docker compose restart gateway
```

After changing environment values, recreate the container:

```bash
docker compose up -d --force-recreate gateway
```

For a source upgrade, build before replacing the running image and retain the
previous image plus private `.env`/`data/` for rollback. Credential paths refer to
container files, so preserve their mounts when recreating the container.

The previous `/tokens`, `/tokens/upload`, `/tokens/clear`, `/tokens/error`,
`/tokens/add/{token}` and `/seed_tokens/clear` routes are removed. The account setup
HTML is no longer shipped. On an API-only deployment those paths return HTTP 404.
Clients using the standard model, Chat Completions or Responses APIs keep the same
base URL and API key. Replace scripts that used the old management routes with
file updates and an explicit restart. Clearing an account/seed pool is an offline
maintenance operation after stopping the service and backing up private state.

An older image still contains the page. Rollback restores that image's behavior;
to remove the page from an existing instance, deploy the new image rather than
only editing `.env`. Token files remain compatible with the older line-oriented
loader when using plain tokens; session JSON and JSON arrays require this version.

## Models and request budgets

`reasoning_effort` uses a local compatibility mapping: `low → min`,
`medium → standard`, `high → extended`, `xhigh → max`. You can also supply the
web `thinking_effort` value using the SDK's `extra_body`. Values must appear in
the selected model's account-specific catalog; conflicting values are rejected.
An explicit different model does not automatically inherit the default model's effort.

| Environment variable | Sample value | Meaning |
| --- | --- | --- |
| `CHATGPT_TOKEN_FILE` | `data/token.txt` | Credential file path inside the container |
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

These values come from `.env.example`; source fallbacks may differ. Continuation
can make additional model requests and consume additional usage. It cannot bypass
an upstream output cap or usage restriction, and it may return a partial answer
with `finish_reason="length"` when a bound is reached.

## Diagnostics

Send a UUID in `X-Client-Request-Id` to correlate requests with `X-Request-Id`.
Bounded lifecycle metadata is stored in `data/lifecycle-audit.jsonl` and
`data/lifecycle-activity.jsonl`. Keep the runtime directory private. Model discovery
HTTP 200 verifies the catalog; only a completed inference verifies a model request.
