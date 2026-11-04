# Validation scope / 验证范围

Validated on **2026-11-04** with real Codex CLI **0.166.0-alpha.5.1**.
The web and native backends are tested and reported separately. Web coding and
stdio MCP below were rerun after the tool-dispatch fix on an independent image
without diagnostic monkeypatches or a source bind. Native rows retain their
earlier qualification; they are not substituted for web results.

| Check | Native `gpt-6.1-terra` | Web `gpt-6-thinking` |
|---|---|---|
| Real CLI exact-answer request | Passed | Passed |
| Real CLI implementation repair | Passed: 4 commands, 1 patch, failing tests became passing; tests unchanged | Passed with High: 2 shell commands, 1 patch, successful client-issued unittest; tests unchanged; CLI exit 0 and completed turn |
| Real CLI Unicode filename with space | Not run | Passed with High: 2 shell commands, 1 patch, successful client unittest; tests unchanged; CLI exit 0 |
| Client-side stdio MCP | Passed: actual random lookup and matching final answer | Passed: actual random lookup and matching final answer |
| Client-side Streamable HTTP MCP | Passed: actual random lookup and matching final answer | Passed with High: 1 actual lookup, exact random result, CLI exit 0 |
| app-server thread/start and follow-up turn | Passed | Passed with High: 2 completed turns in the same thread, exact random fact retained |
| `previous_response_id` continuation | Passed against real upstream | Passed with High: 2 real responses; fact absent from the second input and returned exactly |
| Native V2 compaction and continuation | Passed: native opaque compaction item, then correct retained fact | Not applicable; web compaction is a gateway summary |
| Real CLI WebSocket response | Passed; warmup recorded zero generation requests | Passed with High: 1 WS generation, 0 HTTP generations, 1 generation-free warmup |

The coding acceptance is reproducible through
`scripts/codex_acceptance.py`. Its fixture initially implements subtraction instead
of addition. Acceptance requires the original tests to fail, actual shell and patch items, a
successful client-issued unittest command, independently passing final tests, an
unchanged test-file digest, and CLI exit 0 with a completed turn. Acceptance uses
a temporary Git baseline and a disposable client home, excluding unrelated local
skills/plugins and inherited Desktop session/tool-pipe variables. It runs High
reasoning and does not edit global configuration.
MCP servers generate a random value at startup and write a receipt only when the
client actually executes the lookup. Matching that value establishes an actual
tool round trip, not merely tool declaration or model-generated JSON.

## Web dispatch fix and completed runs

The original failed coding run is retained as the baseline: the upstream chose
ChatGPT-internal `functions.exec` instead of handing client calls to Codex. The
fix separates a remote-client control turn from caller messages and tool-result
data, uses a nonce-bound call/final frame, normalizes only observed presentation
wrappers, and buffers tool-enabled text until complete validation. See the
[root-cause investigation](web-tool-dispatch.md).

On the clean web image, the completed addition-repair fixture took 50.375 seconds:
2 completed shell items, 1 completed patch item, a successful CLI unittest command,
unchanged tests, independently passing final tests, CLI exit 0 and `turn.completed`.
A second clean coding fixture, `姓名 处理.py`, completed in 43.734 seconds with
2 shell items, 1 patch, successful client unittest, unchanged tests and a completed
CLI turn. It fixed boundary whitespace while preserving internal spaces.
The clean stdio MCP fixture completed in 17.984 seconds with 1 real lookup and a
matching randomized return value. These are individual completed runs, not latency
benchmarks or a guarantee that every web request succeeds.

Earlier failed attempts remain failures, including TLS connection failures,
nonconforming final envelopes and an unfinished upstream final message. Only the
non-inference requirements endpoint permits one TLS-connect retry for Responses;
audit metadata counts it separately. Generations are never automatically replayed.
Web protocol, account state and network failures can still interrupt a client turn.
The bridge is not native Codex inference and does not claim complete official API
equivalence. Native Terra remains a separately selected route.

## File-based deployment checks

The account setup page and legacy management routes have been removed. A fresh
Compose project using a synthetic session-JSON credential file started successfully,
served the English API metadata, and returned 404 for the old account routes. The
credential file remained unchanged. This startup check did not access a real account.
Post-upgrade model discovery returned HTTP 200 for both configured backends, and
a real Codex CLI exact-answer request passed with web `gpt-6-thinking` + High.
Both existing local instances were also upgraded with their original environment,
credential mounts and data preserved; the previous images remain available for rollback.

The independent image regression exposed a missing public `.env.example` template
in the build context; the template is now included while private `.env` and runtime
files remain excluded. The configuration-stage image passed 282 tests; the current qualified image
passed 288 tests with networking disabled.

## Completed additional web qualification

All four checks used `gpt-6-thinking` with High through the web backend, on a
built image without source binds or diagnostic injection. Backend/model identities
and generation counts were checked against an isolated instance's server audit.

| Case | Completed-run evidence | Seconds |
| --- | --- | --- |
| Streamable HTTP MCP | Real CLI exit 0 and completed turn; 1 lookup; exact random value; 2 generation POSTs | 19.109 |
| app-server | `thread/start`, then 2 completed turns on the same thread; exact random fact returned; 2 generation POSTs | 15.128 |
| Response-ID continuation | 2 completed HTTP Responses; second input contains only the question and previous ID; exact random fact returned; upstream storage disabled | 16.334 |
| CLI WebSocket | Real CLI exit 0 and completed turn; 1 WS generation; 0 HTTP generations; 1 warmup with 0 generation POSTs | 11.118 |

These are individual completed runs, not a reliability or latency benchmark.
The first continuation check found a confirmed gateway defect: the web terminal
frame was serialized before state commit added storage metadata. First-delivery
JSON/SSE/WS could therefore disagree with replay/retrieval, including reporting
`store=false` for an explicit locally stored response. Terminal frames now contain
the committed response while preserving their existing sequence number. Offline
regressions cover completed/incomplete responses and both storage choices; the
original code failed the new regression before the fix.

A separate app-server attempt failed `invalid_client_tool_envelope`. A final-text-only
diagnostic rerun produced valid frames and completed both turns. The exact earlier
rejected body was unavailable, so no additional parser cause is claimed. The failed
attempt is retained, strict tool validation is unchanged, and the final clean run
above passed. No unknown generation outcome was replayed. A model-catalog read
also failed before inference and was checked again before starting the next case.

Reproduce with `scripts/web_qualification.py` as described in
[Codex compatibility](codex-compatibility.md). The app-server client checks the
[official lifecycle](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server):
initialize, create a thread, start turns and require completed terminal statuses.
Raw model text, account credentials and local diagnostics are not published.

## Regression and image checks

**288 offline tests across 18 suites passed**, with Docker `--network none`.
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
- File-based credentials, session JSON and long values, read-only input, startup
  errors without credential contents, and removed account-management HTTP routes.
- Explicit final/call frames, presentation markers, custom-input preservation,
  prepared connection-pool reuse, and strictly bounded preflight-only TLS retry.

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

网页 `gpt-6-thinking` + High 已在无诊断注入、无源码挂载的独立镜像上通过真实
CLI 编程验收：2 个 Shell 命令、1 次补丁、客户端运行 unittest 成功，测试文件
未改，独立复测通过，CLI 正常结束。中文且含空格的文件名用例也通过相同标准，
实际 stdio MCP 随机值调用与回传通过。
新增四项网页专项也已完成：HTTP MCP 真实调用、app-server 同线程两轮、
previous_response_id 随机事实续接，以及真实 CLI WebSocket（无 HTTP 回退、预热零推理）。
验收发现并修复终态序列化早于提交导致的存储元数据不一致；保留失败尝试记录。
基线失败和中途失败均保留；网页成功独立验证，未使用原生路径替代。
网页压缩仍为网关摘要，与原生加密推理状态不同。

18 个套件共 288 项断网回归通过。离线测试不读取已有账号池及私有 `.env`，
真实模型测试需主动运行并消耗用量，CI 不触发真实推理。一次性客户端目录排除
无关技能、插件和继承的桌面工具管道，不修改全局配置。网页仍可能返回不合协议
或未完成的响应；这些请求明确失败，不靠重发生成或提取思考消息伪装为成功。
