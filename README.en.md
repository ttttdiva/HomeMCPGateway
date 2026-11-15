[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

# Home MCP Gateway

**A tunnel MCP server that lets cloud AI (ChatGPT / Claude Code and claude.ai / Muse) operate your home Windows PC through MCP tools.**

The PC runs one Gateway (92 tools), and each AI service registers it as its own connector (plugin). No inbound ports need to be opened.

```text
ChatGPT        ── OpenAI Secure MCP Tunnel ──┐
Claude Code ─┐                               ├─> Home MCP Gateway ─> Windows PC
claude.ai   ─┴─ HTTPS (e.g. Cloudflare Tunnel)┤   (files / commands / browser /
Muse           ── HTTPS (e.g. Cloudflare Tunnel)┘    desktop / Git / Android ...)
```

> **Warning:** The Gateway has **unrestricted** access to files, processes, network, and desktop with the permissions of the running Windows user. Use it only over private connections that you alone use, and never share its URLs or tokens.

## What it can do

Just ask in chat, and the AI does things like this on your home PC:

| Area | Examples |
| --- | --- |
| Files | Read/write, search, copy/move/delete, hash |
| Commands & processes | Run PowerShell / shell / Python; start, monitor, and stop processes |
| Long-running jobs | Run CLI agents such as Codex in the background, check progress, and steer them |
| Development | Git status, worktree creation, diffs, and cleanup |
| Browser | Playwright automation, attach to a signed-in Edge ([guide](docs/browser-edge-attach.ja.md)) |
| Desktop | Screenshots, screen observation and Computer Use clicks/typing ([details](docs/computer-use.ja.md)) |
| Network | HTTP requests, downloads, waiting for ports/HTTP readiness |
| Android | Tap, type, screenshots, and app management over ADB |

## Common setup

Requires a Windows PC and Python 3.10+.

```powershell
git clone https://github.com/ttttdiva/HomeMCPGateway
Set-Location HomeMCPGateway
.\scripts\setup_windows.ps1
```

Then set up only the AI services you want to use. Each one runs as an auto-starting scheduled task.

## ChatGPT

Uses OpenAI Secure MCP Tunnel (requires a workspace with Developer mode).

1. Extract the **full** Windows ZIP of [tunnel-client](https://github.com/openai/tunnel-client/releases/latest) into the repository, or point `TUNNEL_CLIENT_PATH` in `.env` at it.
2. Create a Tunnel in [OpenAI Tunnels settings](https://platform.openai.com/settings/organization/tunnels) and associate it with your ChatGPT workspace. Create a Restricted key with only **Tunnels: Read / Use** under [API keys](https://platform.openai.com/settings/organization/api-keys).
3. Create `.env` and install the task:

   ```powershell
   Copy-Item .env.example .env   # fill in CONTROL_PLANE_TUNNEL_ID and CONTROL_PLANE_API_KEY
   .\scripts\install_autostart.ps1   # registers and starts "80_HomeMCPGateway - ChatGPT"
   ```

4. In ChatGPT Developer mode, create an app with connection **Tunnel** and authentication **No authentication**. Never enter the API key into ChatGPT.

For a one-off manual connection use `.\scripts\connect_tunnel.ps1`; to restart use `Restart-Gateway.bat`.

## Claude Code (cloud) / claude.ai

Runs an HTTP MCP server on local `127.0.0.1:8766`, exposed through your own HTTPS route (e.g. Cloudflare Tunnel).

```powershell
.\scripts\install_claude_gateway_http_task.ps1   # task "80_HomeMCPGateway - Claude Code"
```

On first run, a secret URL path and Bearer token are generated in `.local/claude-code-mcp/secrets/`. Add an ingress that forwards `/claude-mcp/<path-secret>/mcp` to port 8766, then register the URL from `connector.txt` and the `Authorization: Bearer ...` header under Claude **Customize > Connectors > Custom**. See [Claude Code connection](docs/claude-code-remote-mcp.md).

## Muse

Give Muse the Gateway directly as a custom connector. It runs on local `127.0.0.1:8765`, exposed through an HTTPS route.

```powershell
.\scripts\install_muse_gateway_http_task.ps1   # task "80_HomeMCPGateway - Muse"
```

`path.secret` / `token.secret` must already exist in `.local/muse-grok-native/mcp/secrets/`. For Muse's credential proxy, a Bearer-token-only endpoint `/muse-bearer/mcp` is also available. See [Muse connection](docs/muse-full-gateway.md).

## Scheduled tasks

| Task name | Client | Local |
| --- | --- | --- |
| `80_HomeMCPGateway - ChatGPT` | ChatGPT | tunnel-client |
| `80_HomeMCPGateway - Claude Code` | Claude Code / claude.ai | `127.0.0.1:8766` |
| `80_HomeMCPGateway - Muse` | Muse | `127.0.0.1:8765` |

After changing code or `.env`, restart the relevant task with `Stop-ScheduledTask` → `Start-ScheduledTask`, then refresh the tool list on the AI side.

## AoiTalk integration (optional)

Only if you use AoiTalk: a dedicated MCP (35 tools) can be exposed over a separate Tunnel (`connect_tunnel.ps1 -Target aoitalk`). It is not part of the generic Gateway. See [AoiTalk dedicated MCP](docs/aoitalk-plugin.md).

## More docs

- [Codex launch and WindowsApps handling](docs/codex-app-server.md) — Codex started by the Gateway runs with full access and inherits the full environment
- [Long-running job dispatcher / supervisor](docs/work-dispatcher.md), [resource queue](docs/resource-queue.md)
- [Muse Workspace MCP](docs/muse-workspace-mcp.md) — a separate server that exposes only registered worktrees to Muse

## Troubleshooting

- **Tools missing or stale:** Check that the task is running, then refresh the tool list in the AI's connector.
- **ChatGPT never reaches `ready`:** Check the Tunnel ID, the Restricted key's permissions (Tunnels Read / Use), and the Tunnel–workspace association.
- **Pasted a secret by mistake:** Rotate that key/token immediately and remove it from conversations and logs.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```
