# Security policy

## Supported versions

During the 0.x preview, security fixes target the latest published preview.
Older snapshots and upstream chat2api releases are not maintained by this project.
A public release is not a security certification.

## Private reporting

Use [Report a vulnerability](https://github.com/pythc/chatgpt-web-gateway/security/advisories/new)
for credential disclosure, authentication/isolation bypass, unintended tool
execution, unsafe response state access or another exploitable gateway issue.
Please do not open a public issue with exploitation details or account data.

Include the affected commit/version, minimal synthetic steps, impact and a
proposed fix if available. Use invented credentials. Never provide live tokens,
session JSON, full prompts, signed URLs or private client files. For a private
community conduct concern, use the same private intake and identify it as such;
maintainers will handle it separately from vulnerability advisories.

Reports are handled on a best-effort basis. We will assess reproducibility and
impact, coordinate a fix and agree on disclosure where possible. No bounty or
response deadline is promised. Do not test accounts or systems you do not own.
If private intake is temporarily unavailable, open a public issue asking only
for a private contact, without describing the sensitive concern.

## Trust boundary

The gateway holds operator-provided account credentials and API state. Keep it
bound to loopback unless you deliberately provide an authenticated deployment.
Clients own Shell, patch and MCP execution, sandboxing and approvals. Model output
and tool results are untrusted. Strict frame/schema/grammar validation does not
replace client permissions. See [architecture](docs/architecture.md).
