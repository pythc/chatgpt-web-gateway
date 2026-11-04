# Web Codex tool dispatch investigation

This investigation uses the actual web backend and `gpt-6-thinking`, with High
reasoning, through Codex CLI `0.166.0-alpha.5.1`. Native Terra results are separate.

## Findings and changes

1. **Execution context was ambiguous.** The CLI declared `exec_command` and
   custom `apply_patch`, but a failing web turn produced 19 assistant messages
   addressed to ChatGPT's internal `functions.exec`, followed by internal tool
   messages. Those are not client execution receipts. The CLI received a final
   refusal and executed no workspace commands. The adapter now adds a separate
   remote-client protocol turn, keeping the caller's messages, constraints and
   tool-result data intact. It asks for the next serialized client action or
   final answer, respecting the existing tool choice.
2. **Valid payloads were mixed with web presentation.** Actual final text included
   progress prose and `<escape>` tokens around JSON and individual frame marker
   lines, sometimes without a matching closing token. A whole-text JSON parser
   rejected those responses. Web Responses now use one explicitly delimited
   current-turn frame. Only known presentation tokens at frame/payload boundaries
   are normalized; literal escape tokens inside JSON strings remain unchanged.
3. **A framed reply could be mistaken for ordinary text.** `BEGIN_CLIENT_RESPONSE`
   starts with a letter, so the previous plain-text heuristic could emit it before
   tool validation. Tool-enabled Responses now buffer the entire protocol frame
   before emitting typed client events. Tool-free final text retains live streaming.
4. **Streaming used an unnecessary TLS connection.** Web preparation used one
   curl session, while generation streaming always used a second session. A
   model-free connection probe confirmed that reusing the first pool could use
   zero new connections and zero TLS setup time. Web Responses now stream through
   the prepared session's pool. TLS verification remains enabled, and transport
   failures are not automatically replayed as another generation. Actual failures
   also occurred at `chat_requirements`, with zero generation POSTs and curl code
   35. Only this non-inference token-preparation endpoint permits one TLS-connect
   retry for web Responses, within the original deadline. Audits separately count
   `preflight_connect_retries`; HTTP/auth/quota errors, other transport errors,
   cancellation and generation POSTs are excluded.
5. **The acceptance process loaded unrelated managed plugins.** Despite ignoring
   user config and disabling apps, managed plugins added tool schemas during the
   run. Observed prompts grew to about 26k tokens and triggered real CLI context
   compaction and follow-up requests. Disposable acceptance now also disables
   plugins for that invocation. Ignoring config also leaves installed skills and
   inherited Desktop session/tool-pipe variables in scope. Acceptance now uses a
   disposable client home outside the workspace and removes inherited `CODEX_*`
   variables before selecting that home. Explicit MCP fixtures still load; normal
   user configuration and gateway tool support are unchanged by this isolation.
6. **The coding fixture lacked a Git baseline.** Models legitimately chained
   unittest with `git diff`/status checks. The tests could pass while the compound
   command failed outside a repository. The disposable fixture now has a local
   baseline commit, with hooks and signing disabled for that commit only.

`streaming_parent_patch` was also inspected. The observed events carried logical
answer IDs, parent relationships and revisions, rather than answer-text deltas.
They do not establish that tool JSON was missing from the text decoder. No hidden
reasoning or internal native-tool arguments are promoted into client calls.
The control turn steers serialization; it does not disable ChatGPT's internal
tools through an upstream API switch. Only validated client events count as
client tool calls. Nonconforming replies and unfinished upstream turns remain
explicit failures rather than being repaired by a second generation.

## Validation boundaries

A protocol frame must have one matching pair of current-turn markers, bounded
progress text, no trailing text and exactly one complete JSON object. Duplicate
frames, wrong nonces, truncated JSON, undeclared tools, invalid arguments and
invalid custom grammars remain failures. The adapter never searches arbitrary
prose for a JSON substring to execute.

A coding acceptance requires a successful real CLI turn, actual shell commands,
an actual patch, a successful client-issued unittest command, an unchanged test
file and independently passing final tests. A changed file alone is insufficient.
The second fixture uses a Chinese filename containing a space and a different bug.

See [validation results](validation.md) for the completed-run evidence. Private
credentials, input bodies, final-protocol diagnostics and local receipts remain
outside Git and the image build context.

## 中文摘要

基线并非“模型完全不调用工具”：上游产生了内部 `functions.exec` 消息，但未交付
客户端工具事件。另有可见正文中的进度说明、错位/不成对的 `<escape>` 包装，以及
把协议帧误当普通正文提前流出的缺陷。修复后，调用与最终答案使用当前回合绑定的
完整协议帧，经 JSON、工具参数和 custom grammar 校验后再交付；不从任意正文中
猜测、提取并执行 JSON，不读取思考正文来制造工具调用。

网页 Responses 复用准备阶段已建立的连接，保留 TLS 校验及“不重放未知生成结果”
的边界。验收使用一次性客户端目录，排除无关技能、托管插件及继承的桌面工具管道，
仍实际加载指定 MCP，不修改全局配置。临时工作区补齐 Git 基线，便于正常差异检查。
完整成功标准包含真实 CLI 正常结束、Shell、补丁、客户端运行测试，以及测试文件
未改与独立测试通过；执行到一半或文件已改不计为通过。
