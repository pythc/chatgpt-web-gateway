# Client compatibility

This matrix separates actual model acceptance from synthetic protocol coverage.
Record an exact client version, gateway revision, backend, date and terminal outcome.
A new version remains untested until checked. English/Chinese reports are welcome
through the [compatibility form](https://github.com/pythc/chatgpt-web-gateway/issues/new?template=compatibility_report.yml).

## Maintainer baseline

Qualified before the 0.1.0 release, with results recorded on 2026-11-04 at baseline
`302fe4823e9cfdbc0b8e90167eae8ba4c1c16985`. This is the evidence-recording date;
the exact per-run timestamps remain in private local receipts. Release automation
does not refresh actual model acceptance. Model IDs are account-specific.

| Client / version | Backend | Flow | Status |
| --- | --- | --- | --- |
| Codex CLI 0.166.0-alpha.5.1 | Web, gpt-6-thinking High | Shell, patch, original tests; Chinese/space filename | Live passed |
| Same CLI | Native, gpt-6.1-terra | Shell, patch and original tests | Live passed |
| Same CLI | Web and native | Client-side stdio and Streamable HTTP MCP | Live passed |
| Same CLI / app-server | Web and native | Same-thread follow-up and response-ID continuation | Live passed |
| Same CLI | Web and native | WebSocket response | Live passed |
| Responses protocol / CLI | Web | Compaction | Offline checked; gateway summary |
| Same CLI | Native | Opaque compaction and continuation | Live passed |
| Chat Completions SDK examples | Web | Text and function tools | Earlier live checks; exact SDK version not retained |
| Responses / tool protocol tests | Synthetic backend | Typed events, validation, storage and transport | Offline checked |
| Other client products or versions | Any | All | Untested |
| Hosted API tools on web backend | Web | Hosted search, hosted MCP/code execution | Unsupported |

`Live passed` means a completed bounded run, not a reliability benchmark. MCP
acceptance requires a real lookup and exact randomized result. Coding acceptance
requires client-issued tools, unchanged original tests and independent final tests.
See [validation](validation.md) for commands and [Codex setup](codex-compatibility.md)
for scope, buffering and transport limits.

## Community reports

A report should contain a minimal synthetic case, safe error/status, exact versions,
backend and transport, and one of: live passed, live failed, offline checked,
untested or unsupported. Include a date and public reproduction where possible.
Do not upload raw private receipts or claim another backend's success as web proof.
Maintainers label incoming reports and distinguish community-reported results from
maintainer-qualified entries. Failed attempts are useful evidence too.
