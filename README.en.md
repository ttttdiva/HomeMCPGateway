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

7. Check the initial connection and create the profile. Foreground tunnel startup is described below.

   ```powershell
   .\scripts\connect_tunnel.ps1
   ```

   The script starts the local runtime named `home-mcp`, checks `process_running`, `healthy`, and `ready`, then returns to the prompt. It does not open a tunnel window. The connection is outbound; no inbound firewall port is required.

## ChatGPT-side setup

1. Enable **Developer mode** in ChatGPT.
2. Open the app / connector creation flow and choose **Tunnel** as the connection type.
3. Select the tunnel you created or inspected, or paste its Tunnel ID. Choose **No authentication**. The runtime API key is used only between the local tunnel client and OpenAI; ChatGPT never receives it.
4. Review the tools, create the app / connector, and connect while the local runtime reports `ready`.

See the official [Connect ChatGPT to a remote MCP server guide](https://developers.openai.com/plugins/deploy/connect-chatgpt), [Refresh MCP metadata instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata), and [Using Projects in ChatGPT help article](https://help.openai.com/en/articles/10169521-using-projects-in-chatgpt).

## Normal startup, restart, and windows

### Run the tunnel itself in a visible console

**Use `tunnel-client.exe run` to run the tunnel itself in your PowerShell console.** `runtimes connect` starts or reuses a separately managed process. A visible `connect_tunnel.ps1 -Watch` window belongs to the monitor, not the foreground tunnel process.

The following assumes the initial connection has created `.runtime\profiles\home-mcp.yaml`. First stop the existing monitor and runtime for the same tunnel, using the background-mode stop instructions below. Then open a dedicated PowerShell at the repository root; do not run both modes concurrently.

The existing profile references `env:CONTROL_PLANE_API_KEY`. `run` does not load the repository's `.env`, and `connect_tunnel.ps1` restores its caller's environment when it exits. The following uses the existing script's literal `.env` loading behavior without printing secrets or placing their values in arguments, and restores the environment on exit:

Set the installation directory inside the newly opened PowerShell itself. Change the path below to your actual absolute installation path.

```powershell
$GatewayRoot = 'C:\path\to\HomeMCPGateway'
Set-Location -LiteralPath $GatewayRoot
if (-not (Test-Path -LiteralPath (Join-Path $GatewayRoot '.env') -PathType Leaf)) { throw 'Gateway .env not found.' }
$GatewaySavedEnv = @{}
try {
    foreach ($Line in [IO.File]::ReadAllLines((Join-Path $GatewayRoot '.env'))) {
        $Entry = $Line.Trim()
        if (-not $Entry -or $Entry.StartsWith('#')) { continue }
        if ($Entry -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { throw 'Invalid .env assignment.' }
        $Name = $Matches[1]
        $Value = $Matches[2].Trim()
        if ($Value.Length -ge 2 -and (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or ($Value.StartsWith("'") -and $Value.EndsWith("'")))) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }
        if (-not $GatewaySavedEnv.ContainsKey($Name)) {
            $GatewaySavedEnv[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
        }
        [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    }
    $TunnelClient = $env:TUNNEL_CLIENT_PATH
    if (-not $TunnelClient) {
        $TunnelClient = (Get-ChildItem -Path (Join-Path $GatewayRoot 'tunnel-client-*\tunnel-client.exe') -File |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
    }
    if (-not $TunnelClient -or -not (Test-Path -LiteralPath $TunnelClient -PathType Leaf)) { throw 'Set TUNNEL_CLIENT_PATH in .env.' }
    if (-not $env:CONTROL_PLANE_API_KEY) { throw 'CONTROL_PLANE_API_KEY is required.' }
    & $TunnelClient run --profile-dir (Join-Path $GatewayRoot '.runtime\profiles') --profile home-mcp --log.file=
} finally {
    foreach ($Name in $GatewaySavedEnv.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $GatewaySavedEnv[$Name], 'Process')
    }
}
```

This PowerShell is the tunnel's console. The existing profile logs to a file; use the empty value `--log.file=` for standard output. Do not use `--log.file stdout`: it creates a file named `stdout`. Press Ctrl+C to end the foreground run, then repeat the procedure to reload `.env` and restart. Do not add the secret value to the command. For a different profile name or authentication variable, follow that profile's references.

A direct `run` is not a `runtimes`-managed launch. Do not treat an old managed `stopped` status or `.runtime\supervisor-status.json` as the foreground process's liveness. Check its console and health URL instead. The browser management page is `/ui` under that health URL; the profile's `health.url_file` specifies where the latest URL is written.

### Hidden background operation

If a visible tunnel console is unnecessary and autostart is configured, use the existing `Home MCP Gateway` scheduled task. Choose Run in Task Scheduler, or run:

```powershell
Start-ScheduledTask -TaskName 'Home MCP Gateway'
```

The default task action is `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "<repository>\scripts\connect_tunnel.ps1" -Watch`. It monitors every 30 seconds and reconnects without a visible window. Only if the task is not registered, install it from the repository root; installation starts it immediately:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

| Entry point | Behavior and display |
| --- | --- |
| `tunnel-client.exe run --profile-dir ... --profile home-mcp --log.file=` | Runs the tunnel itself in the current console; load the environment as shown above |
| Existing `Home MCP Gateway` task | Runs `connect_tunnel.ps1 -Watch` continuously in a hidden window |
| `.\scripts\connect_tunnel.ps1` | Connects in process mode, checks status, then returns to the prompt; the runtime continues |
| `.\scripts\connect_tunnel.ps1 -Watch` | Keeps the monitor in the invoking window; the tunnel remains a separately managed process |
| `.\scripts\run_gateway.cmd` | Starts the stdio Gateway itself; does not connect the tunnel or open its management page |

This repository has no batch launcher that opens a dedicated visible tunnel window. `Connected ... using process mode` with `process_running / healthy / ready = True` indicates a background connection, not a visible foreground launch. `connect_tunnel.ps1` reports its management page as `ui_url` and saves it in `.runtime\supervisor-status.json`. Closing a Watch window and stopping its managed runtime are separate operations.

### Background-mode status and restart

Run the following in PowerShell at the repository root. It selects an extracted executable without relying on `PATH`. If `.env` specifies another location through `TUNNEL_CLIENT_PATH`, assign that same full path to `$TunnelClient` instead. PowerShell does not automatically load `.env` values into variables.

```powershell
$TunnelClient = (Get-ChildItem -Path '.\tunnel-client-*\tunnel-client.exe' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
if (-not $TunnelClient) { throw 'Assign the full path of the extracted tunnel-client.exe to $TunnelClient.' }
& $TunnelClient runtimes status home-mcp --json
```

**After changing `.env` or Gateway code, stop and restart the runtime; rerunning the connect command alone is insufficient.** When using the scheduled task, stop its monitoring first, then run the following with the executable selected above:

```powershell
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
& $TunnelClient runtimes stop home-mcp
Start-ScheduledTask -TaskName 'Home MCP Gateway'
```

Startup is asynchronous. Check that all three status fields become True, then refresh the ChatGPT connection metadata. For manual Watch mode, end that monitor first, stop the runtime, and resume with `.\scripts\connect_tunnel.ps1 -Watch`. Replace `home-mcp` with the actual alias if you changed `TUNNEL_ALIAS`.

To also disable autostart, disable and stop the task before stopping the runtime. Re-enable and run the task to resume:

```powershell
Disable-ScheduledTask -TaskName 'Home MCP Gateway'
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
& $TunnelClient runtimes stop home-mcp
```

## AoiTalk (optional)

AoiTalk integration is disabled by default. To enable it, add this to the local `.env`, restart the runtime, and refresh the ChatGPT connection:

```dotenv
HOME_MCP_AOITALK_ENABLED=1
```

AoiTalk itself must already be installed and configured locally, including its endpoint, authentication, and required features. This gateway does not install or configure AoiTalk. The default configuration exposes **76 tools**; enabling AoiTalk exposes **97 tools**. Never send AoiTalk credentials to ChatGPT.

The dedicated AoiTalk MCP connection exposes a separate **35-tool** catalog. During Phase A, the existing 21 tools remain on the generic gateway while the new connection is verified. Configure a separately provisioned Tunnel ID in `HOME_MCP_AOITALK_TUNNEL_ID`; optionally set a dedicated Restricted runtime key in `HOME_MCP_AOITALK_RUNTIME_API_KEY`. After the separate tunnel preflight, use `scripts\connect_tunnel.ps1 -Target aoitalk`. Its alias/profile is `aoitalk`, status and logs live under `.runtime\aoitalk\`, and its profile is `.runtime\profiles\aoitalk.yaml`. To register its separate autostart task, use `scripts\install_autostart.ps1 -Target aoitalk`; the default task name is `Home MCP Gateway - AoiTalk`. These commands do not create a Tunnel or add/refresh a plugin in the GUI.

See [the dedicated AoiTalk MCP guide](docs/aoitalk-plugin.md) for its tools, operation-ID recovery and Docs/Knowledge paging contracts.

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
| AoiTalk (optional) | Clip ingestion, local/API Agent task delegation and final-answer retrieval, and CLI continuation sessions; adds 21 tools |
| Dedicated AoiTalk plugin | Separate 35-tool task/Docs/Knowledge management catalog and runtime |

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
