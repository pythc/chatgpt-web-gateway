# Support

English and Chinese are welcome. This is a community project with best-effort
support, not a hosted service or a guaranteed response-time agreement.

| Need | Where to go |
| --- | --- |
| Installation, configuration or client questions | [Discussions Q&A](https://github.com/pythc/chatgpt-web-gateway/discussions) |
| Reproducible defect | [Bug report](https://github.com/pythc/chatgpt-web-gateway/issues/new?template=bug_report.yml) |
| Client/version compatibility result | [Compatibility report](https://github.com/pythc/chatgpt-web-gateway/issues/new?template=compatibility_report.yml) |
| Scoped improvement | [Feature request](https://github.com/pythc/chatgpt-web-gateway/issues/new?template=feature_request.yml) |
| Security or private safety concern | [Security policy](SECURITY.md) |

## Before reporting

Read [configuration](docs/configuration.md), [compatibility](docs/compatibility.md)
and [validation](docs/validation.md). Use a published version where possible.
Record the exact gateway version/image digest, client version, backend, transport,
model ID and whether the failure happened before or after inference began.

- `401`: check credential validity and which credential the endpoint expects.
- A model catalog response verifies discovery, not generation access.
- `usage_limit`: an upstream quota error; do not repeatedly submit generations.
- Invalid/incomplete tool envelopes: report the safe error code and steps, not raw
  model output. The gateway intentionally rejects unvalidated calls.
- Network failures: state whether the browser, host and container each connect;
  do not assume they share networking.

Keep `.env`, bearer tokens, cookies, account/session JSON, Codex authentication,
raw HTTP headers/bodies, model transcripts and signed URLs private. Replace private
paths, account identifiers and proxy credentials before sharing a small excerpt.
Do not upload the entire `data/` or `diagnostics/` directory. An Issue form does
not automatically redact pasted text. If a secret is exposed, revoke/rotate it
with its provider; deleting the comment alone is insufficient.

Maintainers may request a synthetic reproduction. Unsupported clients can be
reported as untested; a single success does not establish general reliability.
