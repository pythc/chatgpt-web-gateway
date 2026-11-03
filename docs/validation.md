# Validation scope / 验证范围

Validated on **2026-11-04** with real Codex CLI **0.166.0-alpha.5.1**.
The web and native backends are tested and reported separately.

| Check | Native `gpt-6.1-terra` | Web `gpt-6-thinking` |
|---|---|---|
| Real CLI exact-answer request | Passed | Passed |
| Real CLI implementation repair | Passed: 4 commands, 1 patch, failing tests became passing; tests unchanged | **Not passed**: model returned final text without shell/patch calls; implementation remained unchanged |
| Client-side stdio MCP | Passed: actual random lookup and matching final answer | Passed: actual random lookup and matching final answer |
| Client-side Streamable HTTP MCP | Passed: actual random lookup and matching final answer | Not run |
| app-server thread/start and follow-up turn | Passed | Not run |
| `previous_response_id` continuation | Passed against real upstream | Offline protocol checks |
| Native V2 compaction and continuation | Passed: native opaque compaction item, then correct retained fact | Not applicable; web compaction is a gateway summary |
| Real CLI WebSocket response | Passed; warmup recorded zero generation requests | Offline transport checks |

The native coding acceptance is also reproducible through
`scripts/codex_acceptance.py`. Its fixture initially implements subtraction instead
of addition. Acceptance requires the original tests to fail, the final tests to
pass, the test-file digest to remain unchanged, and a completed real CLI turn.
MCP servers generate a random value at startup and write a receipt only when the
client actually executes the lookup. Matching that value establishes an actual
tool round trip, not merely tool declaration or model-generated JSON.

The web coding failure remains a limitation: valid Responses transport and a
passing MCP loop do not guarantee that the web model chooses shell/patch tools
for an agent task. Neither metadata nor text prompts turn this bridge into native
Codex inference. Use the explicitly configured native backend for the qualified
Codex coding workflow; the gateway never substitutes it silently.

## Regression and image checks

**258 offline tests across 15 suites passed**, with Docker `--network none`.
The final image was built successfully and tested independently of the source
bind mount. Existing 221 tests remain included. Coverage includes:

- Function/custom/namespace tools, exact patch strings, Unicode, schema/grammar
  rejection, parallel and out-of-order result associations.
- Actual loopback HTTP/WebSocket transport, authenticated model metadata,
  generation-free warmup, full-context replay, idempotency and local GET/DELETE.
- Owner isolation, encrypted checkpoints, expired state, compaction boundaries,
  native opaque state preservation, and no fallback generation after failure.
- Live final-text timing, explicit rewrite failures, typed terminal events,
  deadlines/disconnects, logical SSE failure audits, quota and safe error metadata.

Run `python scripts/test.py` from the repository root. Suites run in separate
processes and temporary working directories. The runner ignores project dotenv
and inherited Responses credentials/state settings. It does not open the
operator's account pool. Live tests are opt-in and are never run by CI.

Real acceptance also found and fixed Codex's nullable patch-line grammar dialect,
reordered web input echoes, premature validation of intermediate tool text,
terminal-disconnect audit classification, and WS warmups that must not reach
inference. Private credentials, raw model streams/reasoning, signed URLs and local
acceptance logs are excluded from Git and the image build context.

Known `usage_limit` and authentication failures remain failures. An HTTP 200 model
catalog proves discovery, not generation access. No test claims quota recovery,
complete equivalence to every official API feature, or remote cancellation.

## 中文说明

本轮真实 CLI 为 `0.166.0-alpha.5.1`。原生 `gpt-6.1-terra` 已通过实际改代码、
运行测试、stdio/HTTP MCP、app-server 两轮、续接、原生压缩后续接及 WebSocket
应答。编程用例实际执行 4 个命令、1 次补丁，测试文件未改，测试由失败变为通过。

网页 `gpt-6-thinking` 已通过真实 CLI 应答和实际 stdio MCP 随机值回传；
**网页自动编程未通过**：未产生 Shell/补丁调用，文件未修改。这一失败完整保留，
不能用原生路径的成功替代。网页压缩为网关摘要，与原生加密推理状态不同。

15 个套件共 258 项断网回归通过，最终镜像构建并独立验证。离线测试不读取已有
账号池及私有 `.env`，真实模型测试需主动运行并消耗用量，CI 不触发真实推理。
完整传输协议、工具执行回执、测试结果与模型行为分开判定；不宣称完全等同官方 API。
