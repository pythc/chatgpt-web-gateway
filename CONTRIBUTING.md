# Contributing

Thanks for helping improve ChatGPT Web Gateway. English is the default project
language; Chinese issues and contributions are welcome.

## Before you start

- Use Discussions for setup questions and proposals; use Issues for reproducible
  defects and scoped feature requests. Check existing reports first.
- Read [compatibility](docs/compatibility.md) and [support](SUPPORT.md). Account
  availability and upstream quotas are outside the gateway's control.
- For vulnerabilities, use the private channel in [SECURITY.md](SECURITY.md).
- Small documentation, examples and regression-test contributions are useful.
  For architectural changes, discuss the scope before writing a large patch.

## Development

Use Python 3.11 in a virtual environment, install `requirements.txt`, and cache
`cl100k_base` as shown in [validation](docs/validation.md). Optional SDK/MCP
examples use `requirements.examples.txt`.

```sh
python scripts/check_docs.py
python scripts/test.py
```

Or build and test without runtime network access:

```sh
docker build -t chatgpt-web-gateway:test .
docker run --rm --network none --entrypoint python \
  chatgpt-web-gateway:test scripts/test.py
```

The runner uses temporary synthetic account state. Never add real credentials,
`.env`, account files, raw model messages, signed URLs or diagnostic directories.
CI never uses an actual account. Live acceptance is opt-in and consumes usage;
only run it with an account you control and explicit authorization.

## Pull requests

1. Branch from current `main`; keep one coherent change per PR.
2. Explain the trigger, expected behavior, final behavior and validation.
3. Add a regression for a behavior fix. Preserve existing coding-fixture tests.
4. Keep English and Chinese README navigation and deployment steps aligned.
5. Include migration and rollback effects when configuration or state changes.

Maintain strict tool validation, client-owned execution and approvals, explicit
backend/model selection, and no automatic replay of an unknown generation result.
Do not convert reasoning messages or internal web tool activity into client calls.
Self-review the full diff before requesting review. Maintainers may ask for a
smaller scope or more evidence. Merging requires passing CI and maintainer review.

Contributions are covered by this repository's MIT license. Retain upstream
attribution; do not submit code you do not have the right to contribute.

## Maintainer process

Triage with type/backend/needs-info labels, reproduce before assigning a cause,
and close with a reason or a linked fix. Roadmap milestones are goals, not delivery
deadlines. Prefer a small number of well-specified `good first issue` tasks.
Release procedure: [Releasing](docs/releasing.md). Community expectations:
[Code of Conduct](CODE_OF_CONDUCT.md).
