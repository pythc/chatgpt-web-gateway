# Validation scope / 验证范围

Publication checks (2026-11-03): a clean Docker build, **221 offline tests across
13 suites**, isolated startup/HTTP validation with zero generation requests,
configuration generation/overwrite protection, and actual local MCP `tools/list`
and `tools/call` using the bundled example server. The SDK constructors and MCP
result serialization were also checked with the pinned example SDK versions.

The offline suite covers client tool schemas and result associations, continuation
boundaries, model/effort selection, final-answer states, signed WebSocket URL
handling, recovery cursors and budgets, request deadlines/disconnects, and safe
upstream error metadata. Inputs, account strings, and URLs in tests are synthetic.

Run `python scripts/test.py` from the repository root. Each suite runs in its own
process using a temporary working directory. The test runner does not open the
developer's `.env` or configured account pool. For enforced network isolation,
use the `docker run --network none` command in the README.

Function-tool and stdio MCP round trips were exercised against a configured local
gateway during development in October 2026. Those historical checks are not a
promise that an account, model, or current web protocol remains available. This
source publication does not perform fresh ChatGPT generations or upload private
acceptance records. The `examples/` scripts provide explicit live checks for a
user-configured endpoint; running them consumes model usage.

Known upstream `usage_limit` failures remain failures. Neither a passing offline
suite nor a readable model catalog proves that the upstream allocation has reset.

离线测试覆盖工具参数和结果关联、续写边界、模型与强度、最终正文状态、
WebSocket 签名 URL、恢复游标与预算、超时/断开及安全错误元数据。
测试使用临时目录和合成数据，不读取已有账号配置；可通过 Docker 强制断网。
开发期的真实工具/MCP 验证属于历史证据，本次发布不重新发起 ChatGPT 生成，
也不上传私有验收记录。运行示例会消耗模型用量；离线通过不等于上游额度恢复。

2026-11-03 发布检查：从零构建镜像，13 个套件共 221 项离线回归通过；
断网启动/HTTP 检查未产生生成请求；配置生成及防覆盖检查通过；实际执行了
示例 MCP 的工具发现和调用，并核对固定版本 SDK 的构造与结果序列化。
