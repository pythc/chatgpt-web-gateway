# ChatGPT Web Gateway

[English](README.md) | [简体中文](README.zh-CN.md)

把 ChatGPT 网页会话转换为 **OpenAI 兼容的 Chat Completions 和 Codex Responses API**，支持 **Function Calling、客户端 MCP 工具和流式响应**。

沿用标准 OpenAI SDK 工作流：声明 `tools`，接收 `tool_calls`，在客户端执行，再通过 `role: "tool"` 回传结果。对于实现了这套 Chat Completions 工具循环的客户端，可以在下述兼容范围内通过修改 Base URL 和 API Key 接入。

基于 [lanqian528/chat2api](https://github.com/lanqian528/chat2api) 开发，保留原 MIT 许可证。本项目是独立社区项目，不是 OpenAI 官方 API 服务。

## 功能

- OpenAI 兼容的 `POST /v1/chat/completions` 和 `GET /v1/models`。
- **Codex 接入**：Responses SSE、function/custom/namespace 工具、客户端 MCP、有界重放和压缩；明确区分网页与原生后端。[配置与边界](docs/codex-compatibility.zh-CN.md)。
- **标准函数工具**：`tools`、`tool_choice`、`parallel_tool_calls`、assistant `tool_calls`、匹配的 `tool_call_id` 结果回传及后续模型回答。
- **MCP 接入**：客户端连接 MCP 服务器，把工具 Schema 转为函数工具，执行模型请求的 MCP 调用，再回传结果。提供完整 stdio 示例。
- SSE 响应格式，包含工具调用增量、可选 usage 块和 `[DONE]`。
- 从账号查询模型目录、显式选择推理强度，未知模型直接报错，避免静默降级。
- 有界长文本续写、WebSocket/SSE 恢复、请求截止时间及客户端断开后的任务取消。
- 请求 ID 和有界诊断元数据，保留 `usage_limit` 等已识别上游错误码。

## 快速开始

需要 Docker Compose，以及用于生成本地配置的 Python 3。

```bash
git clone https://github.com/pythc/chatgpt-web-gateway.git
cd chatgpt-web-gateway
python3 scripts/configure.py
docker compose up -d --build
```

配置脚本在本机 `.env` 中生成随机客户端 API Key 和 URL 前缀，文件权限为 `0600`，不覆盖已有配置，也不打印凭证。

1. 从本地 `.env` 读取 `API_PREFIX`，访问 `http://127.0.0.1:5005/<API_PREFIX>/tokens`。
2. 在密码输入框中粘贴你自己的 ChatGPT access token，或包含 `accessToken` 的会话 JSON，保存到本机。
3. 配置 API 客户端：

| 配置项 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:5005/<API_PREFIX>/v1` |
| API Key | 本地 `.env` 的 `AUTHORIZATION` |
| Model | `/v1/models` 返回的准确模型 ID |

客户端 API Key 和 ChatGPT 登录 Token 是不同的凭证。登录 Token 留在网关端。`.env`、`data/`、日志和本地诊断材料均不进入 Git 或 Docker 构建上下文。

Compose 仅监听 `127.0.0.1`。继承的账号管理路由使用 URL 前缀，没有独立管理员认证，请保持本机访问。

如网络需要代理，在 `.env` 中设置 `PROXY_URL`。Docker Desktop 可使用 `http://host.docker.internal:7897` 这类地址访问宿主机代理，端口请换成自己的；默认不设置代理。

## 使用标准 OpenAI SDK

在自己的虚拟环境中安装可选示例依赖：

```bash
pip install -r requirements.examples.txt
export OPENAI_BASE_URL='http://127.0.0.1:5005/<API_PREFIX>/v1'
export OPENAI_API_KEY='<本地 .env 中的 AUTHORIZATION>'
export CHATGPT_MODEL='</v1/models 返回的模型 ID>'
python examples/chat.py
```

请求格式与常用 Chat Completions 客户端一致：

```python
from openai import OpenAI

client = OpenAI()  # 读取 OPENAI_BASE_URL 和 OPENAI_API_KEY。
response = client.chat.completions.create(
    model="<从 /v1/models 选择模型 ID>",
    messages=[{"role": "user", "content": "你好！"}],
    max_tokens=1024,
)
print(response.choices[0].message.content)
```

仓库中的示例关闭 SDK 自动重试，避免重放远端结果未知的生成请求。

## Function Calling：标准客户端工具循环

```bash
python examples/tools.py
```

示例用 JSON Schema 声明 `add` 函数，完成以下闭环：

```text
客户端发送 tools + messages
  → 模型请求工具
  → 网关返回 assistant.tool_calls，finish_reason="tool_calls"
  → 客户端执行已声明函数
  → 客户端追加 role="tool"，携带匹配的 tool_call_id
  → 模型给出最终回答
```

支持 `tool_choice="auto"`、`"none"`、`"required"`、指定函数，以及 `parallel_tool_calls`。工具参数根据声明的 JSON Schema 校验。网关负责返回经过校验的调用，**工具由客户端执行**。

内部实现是基于网页模型的文本协议桥接，并非原生 API Function Calling。非法或不完整的工具封装会明确失败，不执行半截调用。客户端协议兼容不代表模型可靠性、延迟也与官方 API 完全相同。

## Codex CLI 与 Responses

真实 Codex CLI 可将网关作为模型提供方，继续使用客户端工具、MCP、沙箱与审批。参见[配置、后端差异和验收命令](docs/codex-compatibility.zh-CN.md)、[provider/MCP 配置](examples/codex_gateway.toml)和 [app-server 示例](examples/codex_app_server.py)。

网页 `gpt-6-thinking` 与原生 `gpt-6.1-terra` 是两条独立路由。兼容协议不代表网页后端拥有所有原生 API 能力。

真实 CLI 编程验收通过的是原生 Terra；网页函数/MCP 已通过，但网页自动 Shell/补丁编程**未通过**，参见[验收记录](docs/validation.md)。

## MCP 工具

```bash
python examples/mcp_client.py
```

命令启动一个无外部副作用的本地 MCP 服务器，并展示：

```text
MCP tools/list → OpenAI tools → 模型 tool_calls
              → MCP tools/call → role="tool" → 最终回答
```

服务器返回随机值，模型需要实际 MCP 执行结果才能得到该值。参见[客户端](examples/mcp_client.py)和[服务器](examples/mcp_server.py)。

本项目把 MCP 放在客户端层：网关接收转换后的函数定义和工具结果，不管理 MCP 服务器连接。客户端可自行处理其他 MCP 传输方式，网关仍使用相同的 Chat Completions 格式。内置示例使用 MCP Python SDK v2（`mcp==2.3.0`）。

## 流式响应与兼容范围

对于 **Chat Completions**，照常传入 `stream=True`。工具模式和自动续写模式会**先缓冲模型输出，再返回 SSE**，用于校验完整工具封装、组装回答；不是上游逐 token 实时透传。工具增量、结束原因和 `[DONE]` 使用 Chat Completions 格式。

| 能力 | 支持范围 |
| --- | --- |
| Chat Completions 文本与历史消息 | 支持 |
| 函数 `tools` 和工具结果回传 | 支持，最多 128 个工具，`n=1` |
| 客户端 MCP 工具 | 通过标准函数工具循环接入 |
| SSE、`stream_options.include_usage` | SSE 支持；usage 块在先缓冲的工具/续写模式下提供 |
| `response_format: {"type":"json_object"}` | 文本请求支持，不能与工具模式同时使用 |
| `max_tokens`、`max_completion_tokens` | 工具/续写模式的本地限制；直接纯文本请用 `max_tokens`；不能抬高上游限制 |
| `reasoning_effort`、`thinking_effort` | 取决于账号及模型，见下方配置 |
| 旧式 `functions` / `function_call` | 拒绝；请用 `tools` / `tool_choice` |
| JSON Schema 远程引用 | 拒绝；只接受本地引用 |
| Responses API / Codex CLI | 在[明确的后端范围](docs/codex-compatibility.zh-CN.md)内支持 |
| 网页后端托管 MCP、API 原生托管搜索/代码执行 | 不支持；使用客户端工具 |

标准 API 客户端可以正常使用已支持的流程，但**不代表完整覆盖 OpenAI 所有接口和内置工具**。

## 模型、输出限制与超时

示例配置使用 `gpt-6-thinking` 和 `high` 强度。这是可修改的配置示例，不保证你的账号当前存在该模型。请先查 `/v1/models`，使用目录中的准确 ID；显式未知模型会报错，不会自动降级。

`reasoning_effort` 使用本地兼容映射：`low → min`、`medium → standard`、`high → extended`、`xhigh → max`。也可以通过 SDK 的 `extra_body` 直接传网页 `thinking_effort`。取值必须在该账号、该模型的目录中；相互冲突的配置会被拒绝。显式选择其他模型时，不会自动继承默认模型的强度。

| 环境变量 | 示例值 | 含义 |
| --- | --- | --- |
| `DEFAULT_MODEL` | `gpt-6-thinking` | 默认请求模型 |
| `DEFAULT_REASONING_EFFORT` | `high` | 默认模型的推理强度 |
| `HISTORY_DISABLED` | `true` | 临时网页会话 |
| `AUTO_CONTINUE` | `true` | 有界自动续写；请求传 `auto_continue=false` 可关闭 |
| `AUTO_CONTINUE_MAX_ROUNDS` | `4` | 最多追加的续写轮次 |
| `AUTO_CONTINUE_MAX_TOKENS` | `65536` | 本地组装输出 Token 上限 |
| `DEFAULT_TOOL_MAX_TOKENS` | `65536` | 工具模式默认本地输出上限 |
| `AUTO_CONTINUE_TIMEOUT` | `600` | 整个请求的工作预算，直接模式也受约束 |
| `CONVERSATION_TIMEOUT` | `180` | 会话传输超时 |
| `WEBSOCKET_IDLE_TIMEOUT` | `15` | 恢复阶段无数据等待时间 |
| `RECOVERY_MAX_SECONDS` | `120` | 恢复阶段时间预算 |
| `RECOVERY_MAX_READS` | `120` | 恢复读取次数上限 |

以上来自 `.env.example`，源码内的回退值可能不同。自动续写会发起额外模型请求并消耗额度；它不能绕过上游输出上限或用量限制，触达边界时可能以 `finish_reason="length"` 返回部分正文。

## 诊断与验证

可在 `X-Client-Request-Id` 中传 UUID，通过响应的 `X-Request-Id` 关联请求。元数据写入 `data/lifecycle-audit.jsonl` 和 `data/lifecycle-activity.jsonl`。请勿发布运行时 `data/` 目录。

`/v1/models` 返回 HTTP 200 只代表模型目录可读取，不代表生成一定成功。上游 SSE 可以先返回 HTTP 200，再发送逻辑错误。识别出的 `usage_limit` 保留在 `trace.lifecycle.upstream_error.code`；它不说明具体额度类别和重置时间。加大超时或修改强度不能作为已经修复的证据。

离线回归使用合成数据，不请求 ChatGPT：

```bash
docker build -t chatgpt-web-gateway:test .
docker run --rm --network none --entrypoint python \
  chatgpt-web-gateway:test scripts/test.py
```

也可在 Python 3.11 环境安装运行依赖后执行：

```bash
pip install -r requirements.txt
python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
python scripts/test.py
```

镜像构建时会缓存 tokenizer，因此容器回归可以断网运行。参见[验证范围](docs/validation.md)及[架构说明](docs/architecture.md)。网页协议、账号可用性和模型行为仍会影响真实请求，离线测试不证明当前上游一定可用。

## 目录

```text
api/          OpenAI 兼容路由、工具桥接、模型目录、自动续写
chatgpt/      网页传输、响应状态机、恢复、请求生命周期管理
utils/        配置、HTTP 客户端、Token 计数
examples/     标准 SDK、函数工具、客户端 MCP 示例
tests/        合成数据离线回归及测试夹具
scripts/      本地配置和可移植测试入口
```

继承的可选网页镜像路由在示例配置中默认关闭。已有本地部署与这份源码发行版相互独立。

## 许可证

[MIT](LICENSE)。上游署名见 [NOTICE](NOTICE)。
