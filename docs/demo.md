# Reproducible demonstration

This is a recording recipe, not a prerecorded or synthetic claim of live success.
Use a disposable project and client home. Never record `.env`, account screens,
raw model traces, real files or credentials. Live requests consume account usage.

## A short terminal walkthrough

1. Show the published release, image digest and selected client version.
2. Show only the names of the local config files; start the prepared image.
3. Use the acceptance fixture for a real coding demonstration:

```sh
python scripts/codex_acceptance.py --case coding --output diagnostics/demo-coding.json
```

4. Require actual Shell and patch items, unchanged original tests, successful
   client tests, independent final tests and a completed turn. Show only a
   manually inspected summary. A failed run should be presented as failed.
5. Demonstrate a real client-side MCP lookup:

```sh
python scripts/codex_acceptance.py --case mcp \
  --mcp-python /absolute/path/to/python-with-example-dependencies \
  --output diagnostics/demo-mcp.json
```

6. Show only the inspected pass/fail summary and mention the selected backend.

Never substitute a native success for a web result. For transport-specific cases,
use [qualification commands](codex-compatibility.md#mcp-and-acceptance). A screencast
should identify its version/date and be reviewed frame by frame before publishing.

## Announcement copy

English: ChatGPT Web Gateway is a self-hosted, file-configured bridge for standard
Chat Completions and Responses clients, with client-side tools/MCP and tested
Codex CLI coding flows. The 0.1.0 preview includes versioned container images,
English/Chinese documentation and reproducible compatibility checks. Web/native
backends have different limits; see the matrix before trying your client.

中文：ChatGPT Web Gateway 是采用文件配置的自托管网关，提供标准客户端可用的
Chat Completions 与 Responses 接口，支持客户端工具和 MCP，并完成指定版本的
Codex CLI 编程验收。0.1.0 为预览版，提供版本镜像、中英文说明和兼容性复现方法。
网页与原生后端能力存在差异，请先查阅兼容矩阵。

Share the release URL, a minimal reproduction and an honest scope statement when
inviting testers. Ask for client/version compatibility reports. Do not promise
full API equivalence or post unsolicited advertisements in other communities.
