# Claude Code (cloud) — Home MCP Gateway

## Architecture
- One existing Cloudflare Tunnel: `aoitalk-home`. **Do not create another Tunnel or cloudflared service.**
- Cloudflare ingress: `<public-host>` `^/claude-mcp/[0-9a-f]{64}/mcp$` to `http://127.0.0.1:8766`.
- Windows task: `80_HomeMCPGateway - Claude Code`.
- Server: `scripts/gateway_muse_http.py --prefix /claude-mcp`, using the same unrestricted `home_mcp_gateway.server` registry as ChatGPT/Muse. Do not copy tools.
- Separate credential files for Claude: `.local/claude-code-mcp/secrets/path.secret`, `token.secret`.
- Claude connector input: `.local/claude-code-mcp/secrets/connector.txt`, **never print or commit**. The directory is Windows-user-owner-only. The file includes the HTTPS URL and a Bearer token.
- Muse `8765` and OpenAI Secure MCP Tunnel stay untouched. No shared credentials.

## Start / install
From the primary repository checkout:

```powershell
.\scripts\install_claude_gateway_http_task.ps1
```

This creates owner-only credentials if absent, starts the task, and enables auto-restart. **When run in elevated PowerShell**, it installs both boot and user-logon triggers. **When run without elevation**, Windows denies the boot trigger; the installer falls back to a user-logon trigger. On this machine the logon trigger is currently installed and running. To add the boot trigger, re-run the same installer once as administrator.

**Windows desktop limitation:** The task uses the user's interactive token (not SYSTEM) to preserve access to their actual desktop. The startup trigger may not launch a process before the user signs in. The logon trigger guarantees startup when the user signs in. For true pre-logon service operation, a separate noninteractive service architecture would be needed, and it would not control the logged-in desktop.

## Claude connector setup (manual user step)
1. Open **Claude Desktop > Customize > Connectors** (or claude.ai).
2. Add **Custom > Web** connector, name: **Home MCP Gateway (Claude)**.
3. Copy the **URL** from `secrets/connector.txt` on the local PC.
4. Choose **No sign in**. Under **Request headers**, enter **Authorization** and the **Bearer ...** value from the same file.
5. Activate the connector in Claude/Claude Code cloud and verify the MCP tool list. Claude desktop local-extension settings are **not** the same as the cloud remote connector.
6. Keep the file and credentials private. The remote connector grants unrestricted operations on the signed-in user's PC.

Anthropic documentation: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp

## Operations and rollback
```powershell
Get-ScheduledTask -TaskName '80_HomeMCPGateway - Claude Code'
Stop-ScheduledTask -TaskName '80_HomeMCPGateway - Claude Code'
Start-ScheduledTask -TaskName '80_HomeMCPGateway - Claude Code'
```

The local listener is **only** 127.0.0.1:8766. HTTP requests require both the exact private path and correct `Authorization: Bearer` header; the authentication token and path secret are never emitted in access logs. Audit records contain only tool name, UTC timestamp, and success/failure.

The Cloudflare rule belongs to repo `85_CaddyGateway` (see `scripts/Manage-ClaudeMcpIngress.mjs`). Remove that specific rule using its rollback action with version verification; do not delete `aoitalk-home`, stop cloudflared, or alter Muse or portal ingress.

Local files:
- `.local/claude-code-mcp/claude-gateway-server.log`
- `.local/claude-code-mcp/claude-gateway-audit.jsonl`
