# Codex 兼容说明

网关负责模型接入。Shell、补丁应用、MCP 连接、Skills、沙箱和审批由真实
Codex 客户端负责；网关不在服务端执行客户端工具。

## 两条明确分开的后端

| | `RESPONSES_BACKEND=web`（默认） | `RESPONSES_BACKEND=native` |
|---|---|---|
| 上游 | ChatGPT 网页会话 | 管理员明确配置的、有权使用的 Responses 端点 |
| 模型 ID | 网页目录中的实际 ID，如 `gpt-6-thinking` | 原生目录中的实际 ID，如 `gpt-6.1-terra` |
| 工具 | 经校验的文本桥接，支持 function、namespace、custom text/grammar | 保留原生 Responses items，无文本工具转换 |
| 输出 | 无工具时 final 正文实时流；含工具回合完整缓冲校验后交付 | 转发上游类型化事件和不透明推理状态 |
| 压缩 | 网关摘要及加密检查点，不保留原生推理状态 | 原生端点或 V2 compaction_trigger，保留规范化输出窗口 |
| 边界 | 仅文本，无托管工具和结构化输出；设有本地上下文上限 | 取决于指定提供方和账号能力 |

网页会话路由拒绝 `-wm` 工作模式模型。目录元数据不会把 Terra 映射成网页模型，
也不会凭空赋予模型工具能力。两条后端均强制上游 `store=false`；原生适配器是
单独的路由，不代表将网页订阅模型变成原生 API 模型。

## 使用真实 CLI

单独安装 Codex。环境变量 `OPENAI_BASE_URL` 填完整网关地址
`http://127.0.0.1:5005/<API_PREFIX>/v1`，`OPENAI_API_KEY` 填网关客户端密钥，
`CHATGPT_MODEL` 填实际可用模型 ID。凭证仅保存在本机环境，不写入提示词或 Git。

```sh
python scripts/codex_run.py --ephemeral --cwd /absolute/path/to/project \
  --sandbox workspace-write '检查失败测试，修复缺陷并验证结果。'
```

启动脚本只为本次调用提供 provider 配置，不修改用户配置、不安装 MCP。
参见 [provider/MCP 配置示例](../examples/codex_gateway.toml)。完整的交互审批与
会话体验请使用正常的 Codex CLI。[app-server 示例](../examples/codex_app_server.py)
演示创建/恢复线程及后续回合；示例会拒绝命令和文件变更审批。使用 `--isolate-mcp` 可在临时验证时避免启动用户已配置的 MCP 服务器。

模型元数据位于受网关密钥保护的 `/v1/codex/models?client_version=0.166.0`。
网页目录默认限定已验证的 `0.166.0` 协议，本地上下文上限 32,768 tokens，
不是对上游模型最大上下文的承诺。升级 CLI 后需要重新验证。

## 原生后端部署

在私有 `.env` 中配置 `RESPONSES_BACKEND=native`、实际
`RESPONSES_UPSTREAM_BASE_URL`、`RESPONSES_UPSTREAM_KEY` 和可选模型白名单。
上游目录须为 Codex Models 格式，普通 OpenAI `/models` 列表不能直接替代。
对于管理员自己的现有 Codex 登录，可显式只读挂载登录文件：

```sh
# 私有 .env 中还需指定原生 base URL。
# CODEX_AUTH_SOURCE 必须指向已存在的 auth.json 绝对路径。
docker compose -f compose.yaml -f compose.codex.yaml up -d --build
```

当前 Codex 订阅端还需显式配置 `RESPONSES_COMPACTION_MODE=responses`，通过
Responses 内的 `compaction_trigger` 请求原生压缩；默认 `endpoint` 使用
`/responses/compact`。失败时不自动换路由重试。

网关不刷新、不修改登录文件，过期后正常返回认证失败。网页 accessToken 与
Codex 登录凭证是两套凭证。共享原生凭证意味着请求使用管理员账号；请绑定
回环地址，只向预期调用者分发网关客户端密钥。

## 状态、重试和传输

- HTTP SSE 包含 created/in-progress、输出 items/deltas 及 completed/incomplete/failed
  终态。截断的工具信封校验失败，不伪装为成功。网页 token 用量明确标记为估算。
- `previous_response_id` 使用网关保存的完整上下文重放，WebSocket 也如此。
  默认仅驻留内存 900 秒，最多 128 条、每条 8 MiB；过期或淘汰返回错误，不新建空回合。
- `store=false` 不写响应检查点，也不允许 GET 检索。`store=true` 允许本地 GET/DELETE；
  如需落盘，还必须配置 `RESPONSES_STATE_DIR` 和稳定私有的 `RESPONSES_STATE_SECRET`。
  AES-GCM 检查点按调用者隔离；过期文件不再读取，管理员应按保留策略清理旧文件。
- `Idempotency-Key` 在同一有限内存窗口内重放已完成结果；pending/unknown 返回 409。
  这不是持久化的恰好执行一次保证。Responses 不自动重试推理、不自动续写正文。
- 可选 `RESPONSES_WEBSOCKET_ENABLED=true` 支持 Bearer 认证及 `response.create`/
  `response.cancel`，每个连接同一时刻仅运行一条响应。不支持 `stream_id` 多路复用，
  也不提供原生上游 WS 的缓存增量语义。`generate:false` 只准备本地请求状态，不调用上游推理。
- 支持总时限、背压、取消和脱敏生命周期日志。取消本地工作不等于远端生成已停止。

## MCP 与验收

stdio、Streamable HTTP MCP 服务器由 Codex 连接，其工具和结果通过 Responses
传递。支持 OAuth 的服务器使用 `codex mcp login <server>`；MCP OAuth 凭证
不交给网关。配置示例见上文 TOML。

以下为主动触发的真实模型验收，会消耗用量，在临时工作区运行，只落盘脱敏摘要。
每次使用不同的输出文件；独占 `.started` 标记防止超时结果未知后误重复执行。

```sh
python scripts/codex_acceptance.py --case hello --output diagnostics/hello.json
python scripts/codex_acceptance.py --case coding --output diagnostics/coding.json
python scripts/codex_acceptance.py --case mcp \
  --mcp-python /absolute/path/to/python-with-example-dependencies \
  --output diagnostics/mcp.json
```

各条真实路由的验证结果见[验收记录](validation.md)。离线协议测试通过不代表当前
上游模型可用，也不代表网页后端与原生 Codex API 在全部能力上完全等价。

## 升级与回滚

保留私有 `.env`、`data/`、原生登录只读挂载和旧镜像摘要。先构建新镜像，再替换
网关容器，并用主动验收脚本检查所选模型路由；不迁移全局 Codex 配置。回滚时
使用旧镜像和保留的私有配置。重启会丢失内存 response ID；持久化检查点需要
相同状态密钥和兼容网关版本，未实现 Responses 的旧版本不能恢复这些检查点。
回滚不删除账号数据或检查点。
