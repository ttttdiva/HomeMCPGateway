[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

# Home MCP Gateway

Home MCP Gateway connects a local MCP server on a Windows PC to ChatGPT through OpenAI Secure MCP Tunnel, exposing local file, process, network, and desktop operations as MCP tools.

> **Important warning:** This gateway has unrestricted access to the local files, processes, network, and desktop available to the Windows user who runs it. Use it only through a trusted private tunnel. Do not expose it to the public internet or untrusted users.

## Requirements

- Windows
- Python 3.10 or newer
- An eligible OpenAI / ChatGPT workspace with access to Tunnel and Developer mode
- PowerShell to run the setup scripts

## PC-side setup

1. Clone or download the public repository.

   ```powershell
   git clone https://github.com/ttttdiva/HomeMCPGateway
   Set-Location HomeMCPGateway
   ```

   Without Git, use **Code → Download ZIP** at the same [public repository](https://github.com/ttttdiva/HomeMCPGateway), then use the extracted folder as the working folder.

2. Create the virtual environment, install dependencies, and run the setup checks.

   ```powershell
   .\scripts\setup_windows.ps1
   ```

3. Download the official [tunnel-client release](https://github.com/openai/tunnel-client/releases/latest). Use the complete Windows **amd64 or arm64 ZIP**; the runtime-only ZIP is not sufficient. Extract `tunnel-client.exe` to a location on `PATH`, to an extracted folder under this repository, or set its full path in `.env` as `TUNNEL_CLIENT_PATH`. Optionally verify the download with the release's `SHA256SUMS.txt` file.

4. Open the [OpenAI Organization Tunnels settings](https://platform.openai.com/settings/organization/tunnels). Create or inspect a tunnel and associate it with the ChatGPT workspace that will use it. Keep these three values distinct:

   - **Tunnel ID** identifies the tunnel. It is not a secret; put it in `.env` as `CONTROL_PLANE_TUNNEL_ID`.
   - **Runtime API key** is the Restricted API key used by `tunnel-client.exe` at runtime. Create it in the next step and store it only in `.env` as `CONTROL_PLANE_API_KEY`.
   - **Admin key** is organization-level administrative authorization for creating, changing, or inspecting tunnels. It is not a runtime key and must not be put in `.env` or ChatGPT.

5. At [Organization API keys](https://platform.openai.com/settings/organization/api-keys), create a **Restricted** runtime API key with **Tunnels: Read** and **Tunnels: Use** permissions. The key is shown only once, so store it securely at creation time. Do not substitute an admin key.

6. Copy the environment template and fill in its placeholders.

   ```powershell
   Copy-Item .env.example .env
   ```

   At minimum, set:

   ```dotenv
   CONTROL_PLANE_TUNNEL_ID=<your-tunnel-id>
   CONTROL_PLANE_API_KEY=<your-restricted-runtime-api-key>
   TUNNEL_ALIAS=home-mcp
   # TUNNEL_CLIENT_PATH=<full-path-to-tunnel-client.exe>
   ```

   Set `TUNNEL_CLIENT_PATH` only when the executable is not found on `PATH` or by the repository-folder search. Keep `.env`, the Tunnel ID, and the runtime API key out of Git, ChatGPT messages, and app configuration.

7. Start the connection.

   ```powershell
   .\scripts\connect_tunnel.ps1
   ```

   The script starts the local runtime named `home-mcp` and checks `process_running`, `healthy`, and `ready`. The connection is outbound; no inbound firewall port is required.

## ChatGPT-side setup

1. Enable **Developer mode** in ChatGPT.
2. Open the app / connector creation flow and choose **Tunnel** as the connection type.
3. Select the tunnel you created or inspected, or paste its Tunnel ID. Choose **No authentication**. The runtime API key is used only between the local tunnel client and OpenAI; ChatGPT never receives it.
4. Review the tools, create the app / connector, and connect while the local runtime reports `ready`.

See the official [Connect ChatGPT to a remote MCP server guide](https://developers.openai.com/plugins/deploy/connect-chatgpt), [Refresh MCP metadata instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata), and [Using Projects in ChatGPT help article](https://help.openai.com/en/articles/10169521-using-projects-in-chatgpt).

## Start, autostart, status, and stop

- Start normally: `.\scripts\connect_tunnel.ps1`
- Watch and reconnect: `.\scripts\connect_tunnel.ps1 -Watch`
- Check status: `tunnel-client.exe runtimes status home-mcp --json`
- Stop the runtime: `tunnel-client.exe runtimes stop home-mcp`

If `tunnel-client.exe` is not on `PATH`, replace the executable name with the extracted full path, or set `TUNNEL_CLIENT_PATH` in `.env`. The `-Watch` mode checks periodically and reconnects an unhealthy runtime.

For autostart and automatic recovery, run this from the repository root. It installs and immediately starts the `Home MCP Gateway` scheduled task for the current Windows user:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

Do not place credentials in task arguments. To stop an autostarted runtime, disable or stop the `Home MCP Gateway` task first, then stop the runtime:

```powershell
Disable-ScheduledTask -TaskName 'Home MCP Gateway'
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
tunnel-client.exe runtimes stop home-mcp
```

Enable the task again and start `connect_tunnel.ps1 -Watch` to resume. After changing `.env`, stop the runtime, run `connect_tunnel.ps1` again, and refresh the ChatGPT connection metadata.

## AoiTalk (optional)

AoiTalk integration is disabled by default. To enable it, add this to the local `.env`, restart the runtime, and refresh the ChatGPT connection:

```dotenv
HOME_MCP_AOITALK_ENABLED=1
```

AoiTalk itself must already be installed and configured locally, including its endpoint, authentication, and required features. This gateway does not install or configure AoiTalk. The default configuration exposes **76 tools**; enabling AoiTalk exposes **84 tools**. Never send AoiTalk credentials to ChatGPT.

## Tool groups

The default configuration exposes 76 tools. The main groups are:

| Group | Examples |
| --- | --- |
| Host and files | System information, environment variables, file reads/writes, search, copy, move, delete, hashing |
| Commands and processes | Arbitrary shell / PowerShell / Python, process start, inspection, and termination |
| Network | Arbitrary HTTP(S) requests, downloads, and TCP / HTTP readiness checks |
| Desktop and devices | Windows observation/UI actions, screenshots, and Android / ADB |
| Development workflows | Git repositories / worktrees, persistent jobs, and browser sessions (Playwright) |
| Images and diagnostics | Image context plus local tool-timing diagnostics |
| AoiTalk (optional) | Clip ingestion and local task delegation; adds 8 tools |

These groups do not create an allowlist or a read-only boundary. Limit connections and prompts to people and workflows you trust.

## Troubleshooting

- `Python 3.10+ was not found`: install Python 3.10 or newer, reopen PowerShell, and run `setup_windows.ps1` again.
- `tunnel-client.exe` not found: extract the complete Windows ZIP (not the runtime-only package), put the executable on `PATH`, or set `TUNNEL_CLIENT_PATH`.
- Runtime is not `ready`: check the Tunnel ID, the Restricted runtime key's Tunnels Read / Use permissions, the tunnel-to-workspace association, and the local runtime status.
- ChatGPT shows no tools: verify Developer mode, **Tunnel**, the correct Tunnel ID, **No authentication**, and local `ready` status; restart the gateway and use [Refresh MCP metadata](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata).
- AoiTalk tools are missing: set `HOME_MCP_AOITALK_ENABLED=1`, restart the runtime, verify that AoiTalk is installed and configured, then refresh the connection.
- A credential was pasted into ChatGPT: rotate the runtime API key immediately and remove it from conversations, app settings, and shared logs. ChatGPT does not need this key.

This is an outbound tunnel, so opening a Windows inbound firewall rule is not a troubleshooting step.

## Development tests

The setup script runs the standard library test suite. Run it again with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For the optional pytest suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

Do not pass local credentials as test arguments or to ChatGPT when testing against a real Tunnel or AoiTalk service.
