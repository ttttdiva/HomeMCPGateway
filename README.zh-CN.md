[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

# Home MCP Gateway

**一个隧道 MCP 服务器，让云端 AI（ChatGPT / Claude Code 与 claude.ai / Muse）通过 MCP 工具操作家中的 Windows PC。**

PC 上运行同一个 Gateway（92 个工具），各 AI 服务分别将其注册为自己的连接器（插件）。无需开放任何入站端口。

```text
ChatGPT        ── OpenAI Secure MCP Tunnel ──┐
Claude Code ─┐                               ├─> Home MCP Gateway ─> Windows PC
claude.ai   ─┴─ HTTPS（Cloudflare Tunnel 等）─┤   （文件 / 命令 / 浏览器 /
Muse           ── HTTPS（Cloudflare Tunnel 等）─┘    桌面 / Git / Android ...）
```

> **警告：** Gateway 以当前 Windows 用户的权限**不受限制地**访问文件、进程、网络和桌面。只应通过仅供自己使用的私有连接使用，绝不要共享其 URL 或令牌。

## 能做什么

只需在聊天中提出请求，AI 就会在家中 PC 上完成如下工作：

| 类别 | 示例 |
| --- | --- |
| 文件 | 读写、搜索、复制/移动/删除、哈希 |
| 命令与进程 | 运行 PowerShell / shell / Python，启动、监控、结束进程 |
| 长时间作业 | 在后台运行 Codex 等 CLI 代理，查看进度并追加指示 |
| 开发 | Git 状态、创建 worktree、差异与清理 |
| 浏览器 | Playwright 操作、附加到已登录的 Edge（[指南](docs/browser-edge-attach.ja.md)） |
| 桌面 | 截图、屏幕观察以及点击/输入等 Computer Use（[详情](docs/computer-use.ja.md)） |
| 网络 | HTTP 请求、下载、等待端口/HTTP 就绪 |
| Android | 通过 ADB 点击、输入、截图和应用管理 |

## 通用准备

需要 Windows PC 和 Python 3.10 及以上。

```powershell
git clone https://github.com/ttttdiva/HomeMCPGateway
Set-Location HomeMCPGateway
.\scripts\setup_windows.ps1
```

之后只需配置要使用的 AI 服务。每个服务都作为自动启动的计划任务运行。

## ChatGPT

使用 OpenAI Secure MCP Tunnel（需要可使用 Developer mode 的工作区）。

1. 将 [tunnel-client](https://github.com/openai/tunnel-client/releases/latest) 的 Windows **完整版 ZIP** 解压到仓库中，或在 `.env` 的 `TUNNEL_CLIENT_PATH` 中指定。
2. 在 [OpenAI Tunnels 设置](https://platform.openai.com/settings/organization/tunnels) 中创建 Tunnel 并关联 ChatGPT 工作区；在 [API keys](https://platform.openai.com/settings/organization/api-keys) 中创建仅有 **Tunnels: Read / Use** 的 Restricted key。
3. 创建 `.env` 并安装任务：

   ```powershell
   Copy-Item .env.example .env   # 填写 CONTROL_PLANE_TUNNEL_ID 和 CONTROL_PLANE_API_KEY
   .\scripts\install_autostart.ps1   # 注册并启动“80_HomeMCPGateway - ChatGPT”
   ```

4. 在 ChatGPT Developer mode 中创建应用，连接方式选 **Tunnel**，认证选 **No authentication**。不要把 API key 输入 ChatGPT。

临时手动连接用 `.\scripts\connect_tunnel.ps1`，重启用 `Restart-Gateway.bat`。

## Claude Code（云端）/ claude.ai

在本地 `127.0.0.1:8766` 运行 HTTP MCP 服务器，并通过自己的 HTTPS 路径（如 Cloudflare Tunnel）公开。

```powershell
.\scripts\install_claude_gateway_http_task.ps1   # 任务“80_HomeMCPGateway - Claude Code”
```

首次运行会在 `.local/claude-code-mcp/secrets/` 中生成秘密 URL 路径和 Bearer 令牌。配置将 `/claude-mcp/<path-secret>/mcp` 转发到 8766 的 ingress，然后在 Claude 的 **Customize > Connectors > Custom** 中登记 `connector.txt` 里的 URL 和 `Authorization: Bearer ...` 头。详见 [Claude Code 连接](docs/claude-code-remote-mcp.md)。

## Muse

直接把 Gateway 作为 Muse 的自定义连接器。它运行在本地 `127.0.0.1:8765`，通过 HTTPS 路径公开。

```powershell
.\scripts\install_muse_gateway_http_task.ps1   # 任务“80_HomeMCPGateway - Muse”
```

需要事先在 `.local/muse-grok-native/mcp/secrets/` 中准备 `path.secret` / `token.secret`。针对 Muse 的凭据代理，还提供仅用 Bearer 令牌认证的 `/muse-bearer/mcp`。详见 [Muse 连接](docs/muse-full-gateway.md)。

## 计划任务

| 任务名 | 连接对象 | 本地 |
| --- | --- | --- |
| `80_HomeMCPGateway - ChatGPT` | ChatGPT | tunnel-client |
| `80_HomeMCPGateway - Claude Code` | Claude Code / claude.ai | `127.0.0.1:8766` |
| `80_HomeMCPGateway - Muse` | Muse | `127.0.0.1:8765` |

修改代码或 `.env` 后，用 `Stop-ScheduledTask` → `Start-ScheduledTask` 重启相应任务，然后在 AI 端刷新工具列表。

## AoiTalk 集成（可选）

仅在使用 AoiTalk 时：可通过单独的 Tunnel 公开专用 MCP（35 个工具）（`connect_tunnel.ps1 -Target aoitalk`）。它不属于通用 Gateway。详见 [AoiTalk 专用 MCP](docs/aoitalk-plugin.md)。

## 其他文档

- [Codex 启动与 WindowsApps 处理](docs/codex-app-server.md) — 由 Gateway 启动的 Codex 以完全访问权限运行并继承全部环境变量
- [长时间作业 dispatcher / supervisor](docs/work-dispatcher.md)、[resource queue](docs/resource-queue.md)
- [Muse Workspace MCP](docs/muse-workspace-mcp.md) — 仅向 Muse 公开已登记 worktree 的独立服务器

## 故障排除

- **工具不显示或过旧：** 确认任务正在运行，然后在 AI 的连接器中刷新工具列表。
- **ChatGPT 一直未 `ready`：** 检查 Tunnel ID、Restricted key 的权限（Tunnels Read / Use）以及 Tunnel 与工作区的关联。
- **误粘贴了秘密信息：** 立即轮换该密钥/令牌，并从对话和日志中删除。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```
