# Changelog

Version numbers belong to ChatGPT Web Gateway, independently of upstream
chat2api. All 0.x versions are previews. Published releases are listed on
[GitHub Releases](https://github.com/pythc/chatgpt-web-gateway/releases).

## 0.1.0 — 2026-11-04

First project preview, based on the qualified gateway implementation.

- OpenAI-compatible Chat Completions and Responses, streaming, validated client
  function/custom/namespace tools and client-side MCP integration.
- Codex CLI coding, stdio/HTTP MCP, app-server, response continuation and optional
  WebSocket flows, within the documented backend-specific scope.
- File-based deployment, private credential mounts, explicit backend/model choice,
  bounded recovery and diagnostics. Account-management HTTP pages are removed.
- English-first documentation, Chinese README, community contribution/reporting
  paths, compatibility reporting, and versioned container release automation.

### Migration and limitations

- Gateway version is now `0.1.0`; the inherited `1.8.8-beta2` string referred to
  upstream history, not this project's release sequence.
- Standard client base URLs, keys and token-file paths remain configurable.
- Replace old `/tokens*` administration scripts with credential-file updates and
  a service restart. Plain tokens remain compatible with the older loader;
  session JSON/arrays require this implementation.
- Web tools use a validated text bridge. Web compaction is a gateway summary;
  native opaque compaction is a separate path. No complete hosted-API parity,
  model availability, quota recovery or remote cancellation guarantee is made.
- Real model acceptance comes from the qualified baseline, not from release CI.
  See [validation](docs/validation.md) and [compatibility](docs/compatibility.md).
