# Architecture / 架构

```mermaid
flowchart LR
    A[OpenAI-compatible client] --> B[Chat Completions API]
    B --> C[Function schema and history bridge]
    C --> D[ChatGPT web transport]
    D --> E[Final-response state machine]
    E --> F[Validated text or tool_calls]
    F --> A
    A --> G[Client functions or MCP server]
    G -->|role=tool| A
```

`api/client_tools.py` serializes declarations and historical tool data into a
nonce-bound text protocol. It validates returned names, argument schemas,
parallel-call constraints, and call/result associations. It never executes a
function. MCP server discovery and invocation belong to the client.

`chatgpt/webStream.py` tracks final-channel responses separately from other
message channels. `ChatService.py` handles transport and bounded read recovery.
`api/continuation.py` assembles explicit continuations and stops on unverified
boundaries. Recovery reads are distinct from generation requests.

`chatgpt/requestScope.py` owns an individual request's deadline and cancellation.
It records whether local work finished; local cancellation does not prove that
an upstream generation stopped. Diagnostic metadata avoids retaining prompts,
responses, credential-bearing URLs, or arbitrary error text in lifecycle logs.

工具声明、历史调用及结果经过文本协议桥接；只有客户端执行函数或 MCP 调用。
状态机区分最终正文与其他消息通道；传输恢复读取与新生成请求分别计数。
请求生命周期控制本地任务、超时及取消，但本地取消不等于远端生成已经停止。
