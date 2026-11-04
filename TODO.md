# Codex compatibility implementation

Baseline: `0169084`. Preserve the existing Chat Completions API, account privacy
defaults, and existing deployments. Check items only after behavior is verified.

- [x] 1. Freeze the Codex CLI request contract and verify the exact Terra/model route.
- [x] 2. Add Responses request/item normalization and function/custom tool round trips.
- [x] 3. Implement incremental Responses SSE, terminal states, and safe text streaming.
- [x] 4. Extend request deadlines, cancellation, and redacted errors to Responses.
- [x] 5. Add authenticated Codex model metadata without changing existing model discovery.
- [x] 6. Separate coding turns from text continuation; preserve call/result IDs and replay.
- [x] 7. Add bounded session checkpoints, optional incremental continuation, and context compaction.
- [x] 8. Add Codex CLI/app-server integration and stdio/HTTP MCP examples; retain client execution and approval boundaries.
- [x] 9. Add regression cases for patches, Unicode, parallel/out-of-order tools, disconnections, and quota errors.
- [x] 10. Run self-review, offline suites, and real Codex CLI coding/MCP/continuation acceptance; record exact backend/model and limitations.
- [x] 11. Update English and Chinese documentation and CI, then commit and push the completed implementation.

## Evidence policy

Synthetic CLI/protocol checks and actual upstream model checks are separate.
Never call a native backend or another model Terra without verifying its route.
Unknown remote generation outcomes are not automatically replayed. Do not publish
tokens, local account files, request bodies, raw reasoning, or private diagnostics.

## Current verified evidence

- CLI `0.166.0-alpha.5.1`: captured actual `input.additional_tools`, namespace/custom
  tools, nullable-line apply_patch grammar, reasoning controls and `store=false`.
- Native `gpt-6.1-terra`: real CLI hello, implementation repair with unchanged tests,
  and actual MCP lookup passed. Gateway audits report completed requests as 200.
- Native `previous_response_id`, V2 compaction, and post-compaction continuation passed.
- Web `gpt-6-thinking`: real CLI hello passed after fixing repeated historical user
  echoes. Real stdio MCP passed. Web-only coding now passed on a clean image: 2 shell commands, 1 patch, client unittest, unchanged tests, and a completed turn; native success does not replace it.
- 276 offline tests across 16 suites passed, including actual loopback HTTP/WS,
  ownership, replay, compaction boundaries, grammars, streaming, and disconnects.
- Item 8 covers integration/examples; live app-server and web MCP checks are tracked
  under item 10. MCP OAuth/HTTP authentication is handled by the Codex client.

Final acceptance and remaining web protocol boundaries: [validation.md](docs/validation.md).


## Web shell / patch root-cause investigation

- [x] Capture actual CLI input and upstream message-channel metadata on a separate web candidate.
- [x] Isolate the cause with a controlled web-only experiment; preserve model and effort.
- [x] Implement the validated fix and review tool/result and instruction boundaries.
- [x] Pass real Codex CLI shell, patch, unchanged-test and multi-turn acceptance.
- [x] Run 276 offline tests across 16 suites and repeat coding/MCP on a clean image.
- [x] Pass a second coding fixture with a Chinese filename containing a space.
- [x] Update bilingual evidence and document remaining protocol/network boundaries.
- [x] Publish the reviewed fix and verify GitHub CI.
