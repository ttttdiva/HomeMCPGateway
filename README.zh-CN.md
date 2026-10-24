[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

# Home MCP Gateway

Home MCP Gateway 通过 OpenAI Secure MCP Tunnel，把 Windows PC 上运行的本地 MCP 服务器连接到 ChatGPT，将本机文件、进程、网络和桌面操作作为 MCP 工具提供出来。

> **重要警告：** 此 Gateway 可以不受限制地访问运行它的 Windows 用户能够访问的本地文件、进程、网络和桌面。只能通过可信的私有隧道使用；不要向公共互联网或不受信任的用户开放。

## 要求

- Windows
- Python 3.10 或更高版本
- 有权使用 Tunnel 和 Developer mode 的合资格 OpenAI / ChatGPT 工作区
- 可运行安装脚本的 PowerShell

## PC 端设置

1. 克隆或下载公开仓库。

   ```powershell
   git clone https://github.com/ttttdiva/HomeMCPGateway
   Set-Location HomeMCPGateway
   ```

   如果不使用 Git，请在同一个[公开仓库](https://github.com/ttttdiva/HomeMCPGateway)中选择 **Code → Download ZIP**，然后将解压后的文件夹作为工作目录。

2. 创建虚拟环境、安装依赖并运行设置检查。

   ```powershell
   .\scripts\setup_windows.ps1
   ```

3. 从官方 [tunnel-client 发布页](https://github.com/openai/tunnel-client/releases/latest)下载 Windows **amd64 或 arm64 的完整 ZIP**；runtime-only ZIP 不够用。将解压出的 `tunnel-client.exe` 放到 PATH 中、放到本仓库下的解压目录中，或在 `.env` 中通过 `TUNNEL_CLIENT_PATH` 指定完整路径。可以选择使用该发布版本的 `SHA256SUMS.txt` 文件校验下载内容。

4. 打开 [OpenAI Organization 的 Tunnels 设置](https://platform.openai.com/settings/organization/tunnels)，创建或查看 Tunnel，并将它关联到要使用的 ChatGPT 工作区。请区分以下三个值：

   - **Tunnel ID** 用于标识 Tunnel。它不是秘密值，放入 `.env` 的 `CONTROL_PLANE_TUNNEL_ID`。
   - **Runtime API key** 是 `tunnel-client.exe` 运行时使用的 Restricted API key。下一步创建它，并且只将它保存在 `.env` 的 `CONTROL_PLANE_API_KEY` 中。
   - **Admin key** 是用于在组织层面创建、修改或查看 Tunnel 的管理授权。它不是 runtime key，不要放入 `.env` 或 ChatGPT。

5. 在 [Organization API keys](https://platform.openai.com/settings/organization/api-keys) 中创建 **Restricted** runtime API key，只授予 **Tunnels: Read** 和 **Tunnels: Use** 权限。密钥只显示一次，请在创建时安全保存；不要用 admin key 代替它。

6. 复制环境变量模板并填写占位符。

   ```powershell
   Copy-Item .env.example .env
   ```

   至少设置以下内容：

   ```dotenv
   CONTROL_PLANE_TUNNEL_ID=<your-tunnel-id>
   CONTROL_PLANE_API_KEY=<your-restricted-runtime-api-key>
   TUNNEL_ALIAS=home-mcp
   # TUNNEL_CLIENT_PATH=<full-path-to-tunnel-client.exe>
   ```

   只有当 PATH 或仓库目录搜索无法找到可执行文件时，才需要设置 `TUNNEL_CLIENT_PATH`。不要把 `.env`、Tunnel ID 或 runtime API key 提交到 Git，也不要放进 ChatGPT 消息或应用配置。

7. 检查首次连接并创建profile。隧道本体的前台启动方法见下文。

   ```powershell
   .\scripts\connect_tunnel.ps1
   ```

   此脚本会启动名为 `home-mcp` 的本地 runtime，检查 `process_running`、`healthy` 和 `ready`，然后返回命令提示符。它不会自动打开隧道窗口。连接是出站连接，不需要开放入站防火墙端口。

## ChatGPT 端设置

1. 在 ChatGPT 中启用 **Developer mode**。
2. 打开应用 / connector 创建流程，选择 **Tunnel** 作为连接类型。
3. 选择刚刚创建或查看的 Tunnel，或粘贴 Tunnel ID。选择 **No authentication**。Runtime API key 只在本地 tunnel-client 与 OpenAI 之间使用，ChatGPT 永远不会收到它。
4. 检查显示的工具，创建应用 / connector，并在本地 runtime 报告 `ready` 时连接。

请参阅官方 [Connect ChatGPT to a remote MCP server 指南](https://developers.openai.com/plugins/deploy/connect-chatgpt)、[Refresh MCP metadata 说明](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)以及 [Using Projects in ChatGPT 帮助文章](https://help.openai.com/en/articles/10169521-using-projects-in-chatgpt)。

## 日常启动、重启与窗口显示

### 在可见控制台运行隧道本体

**要在PowerShell控制台中运行隧道本体，请使用 `tunnel-client.exe run`。** `runtimes connect` 会启动或复用单独管理的进程。`connect_tunnel.ps1 -Watch` 的可见窗口属于监视程序，不是隧道本体的前台控制台。

以下假定首次连接已创建 `.runtime\profiles\home-mcp.yaml`。先按下文的后台停止步骤，结束同一隧道的监视和runtime，再在仓库根目录打开独立PowerShell；不要同时运行两种模式。

现有profile通过 `env:CONTROL_PLANE_API_KEY` 引用认证信息。`run` 不会加载仓库的 `.env`，而 `connect_tunnel.ps1` 会在结束时恢复调用方环境。以下代码按现有脚本的方式将 `.env` 作为纯文本加载，不显示秘密值，不将其写入命令参数，并在退出时恢复环境：

请在新打开的PowerShell本身中指定安装目录。请将下面的路径改为实际绝对路径。

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

此PowerShell就是隧道本体的控制台。现有profile将日志写入文件；使用空值 `--log.file=` 输出到控制台。不要使用 `--log.file stdout`，它会创建名为 `stdout` 的文件。按Ctrl+C结束前台运行，重启时重复该步骤以重新加载 `.env`。不要把秘密值加入命令。如使用其他profile名称或认证环境变量，请按对应profile的引用调整。

直接 `run` 不属于 `runtimes` 管理启动。不要将管理侧旧的 `stopped` 状态或 `.runtime\supervisor-status.json` 当作前台进程的存活状态；应检查本体控制台和health URL。浏览器管理页面是该health URL下的 `/ui`；profile中的 `health.url_file` 指定最新URL的保存位置。

### 隐藏的后台运行

如果不需要隧道本体的可见控制台，且已配置自动启动，可使用现有的 `Home MCP Gateway` 计划任务。在任务计划程序中选择“运行”，或执行：

```powershell
Start-ScheduledTask -TaskName 'Home MCP Gateway'
```

任务的默认操作为 `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "<repository>\scripts\connect_tunnel.ps1" -Watch`。它在隐藏窗口中每30秒检查一次，并在异常时重连。仅在任务尚未注册时，从仓库根目录执行以下命令；注册后会立即启动任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

| 入口 | 行为与显示 |
| --- | --- |
| `tunnel-client.exe run --profile-dir ... --profile home-mcp --log.file=` | 在当前控制台前台运行隧道本体；需按上文加载环境 |
| 现有 `Home MCP Gateway` 任务 | 在隐藏窗口中持续执行 `connect_tunnel.ps1 -Watch` |
| `.\scripts\connect_tunnel.ps1` | 以process mode连接、检查状态后返回提示符；runtime继续运行 |
| `.\scripts\connect_tunnel.ps1 -Watch` | 在当前窗口持续执行监视程序；本体是单独管理的进程 |
| `.\scripts\run_gateway.cmd` | 启动stdio Gateway本体；不会连接隧道或打开管理页面 |

本仓库没有打开独立可见隧道窗口的批处理启动器。显示 `Connected ... using process mode` 且 `process_running / healthy / ready = True` 表示后台连接正常，不代表本体已在可见控制台启动。`connect_tunnel.ps1` 的管理页面URL通过 `ui_url` 输出，并保存在 `.runtime\supervisor-status.json` 中。关闭Watch窗口与停止其管理的runtime是不同操作。

### 后台模式的状态检查与重启

在仓库根目录的PowerShell中执行以下命令。它选择已解压的可执行文件，不依赖PATH。如果 `.env` 的 `TUNNEL_CLIENT_PATH` 指定了其他位置，请将同一完整路径赋给 `$TunnelClient`。PowerShell不会自动将 `.env` 的值读入变量。

```powershell
$TunnelClient = (Get-ChildItem -Path '.\tunnel-client-*\tunnel-client.exe' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
if (-not $TunnelClient) { throw '请将已解压的tunnel-client.exe完整路径赋给 $TunnelClient。' }
& $TunnelClient runtimes status home-mcp --json
```

**修改 `.env` 或Gateway代码后，需要先停止再启动runtime；仅重新执行连接命令不够。** 使用计划任务时，先停止监视，再按以下顺序操作（使用上文已确认的可执行文件）：

```powershell
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
& $TunnelClient runtimes stop home-mcp
Start-ScheduledTask -TaskName 'Home MCP Gateway'
```

启动是异步的。确认三个状态字段均变为True后，再刷新ChatGPT连接元数据。手动Watch模式下，应先结束监视、停止runtime，再执行 `.\scripts\connect_tunnel.ps1 -Watch`。如已修改 `TUNNEL_ALIAS`，请将示例中的 `home-mcp` 替换为实际alias。

如需同时禁用自动启动，先禁用并停止任务，再停止runtime。恢复时重新启用并运行任务：

```powershell
Disable-ScheduledTask -TaskName 'Home MCP Gateway'
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
& $TunnelClient runtimes stop home-mcp
```

## AoiTalk（可选）

AoiTalk 工具仅通过 AoiTalk 专用 MCP 连接提供。Phase B 之后，通用 Gateway 始终公开 **78 个工具**，不再注册旧的 `aoitalk_*` 工具；`HOME_MCP_AOITALK_ENABLED` 是无效的旧 flag，不影响其 catalog。撤除的工具名称与 offline catalog 契约记录在 [Phase B catalog 变更](docs/aoitalk-plugin.md) 中。专用连接提供 **35 个工具**（Clip 3、local task 5、API Agent 5、CLI session 8、管理工具14）。不要将 AoiTalk 凭据发送给 ChatGPT。

AoiTalk 本身及其端点、认证和所需功能必须另行配置。本 Gateway 不会安装或配置 AoiTalk。将单独配置的 Tunnel ID 写入 `HOME_MCP_AOITALK_TUNNEL_ID`；如需独立 Restricted runtime key，可设置 `HOME_MCP_AOITALK_RUNTIME_API_KEY`。完成单独的隧道 preflight 后，使用 `scripts\connect_tunnel.ps1 -Target aoitalk`。它使用独立 alias/profile `aoitalk`，状态和日志位于 `.runtime\aoitalk\`，profile 文件位于 `.runtime\profiles\aoitalk.yaml`。如需注册独立的自动启动任务，使用 `scripts\install_autostart.ps1 -Target aoitalk`；默认任务名为 `Home MCP Gateway - AoiTalk`。这些命令不会创建 Tunnel，也不会在 GUI 中添加或刷新 Plugin。

工具、operation ID 状态恢复以及 Docs/Knowledge 分页契约见[专用 AoiTalk MCP 指南](docs/aoitalk-plugin.md)。

## 工具分组

通用 Gateway 始终公开 78 个工具，主要分组如下：

| 分组 | 示例 |
| --- | --- |
| 主机与文件 | 系统信息、环境变量、文件读写、搜索、复制、移动、删除、哈希 |
| 命令与进程 | 任意 shell / PowerShell / Python、进程启动、检查和终止 |
| 网络 | 任意 HTTP(S) 请求、下载以及 TCP / HTTP readiness 检查 |
| 桌面与设备 | Windows 观测/UI 操作、截图以及 Android / ADB |
| 开发工作流 | Git 仓库 / worktree、持久化任务以及浏览器会话（Playwright） |
| 图像与诊断 | 图像上下文和本地工具计时诊断 |

单独的 AoiTalk 专用 MCP 连接提供35个工具（Clip 3、local task 5、API Agent 5、CLI session 8、管理工具14）。这些工具不计入通用 Gateway 的78个工具。

这些分组不会额外建立 allowlist 或 read-only 边界。请只将连接和提示交给可信的人和工作流。

## 故障排查

- 出现 `Python 3.10+ was not found`：安装 Python 3.10 或更高版本，重新打开 PowerShell，再次运行 `setup_windows.ps1`。
- 找不到 `tunnel-client.exe`：解压完整 Windows ZIP（不是 runtime-only 包），将可执行文件放入 PATH，或设置 `TUNNEL_CLIENT_PATH`。
- runtime 没有变成 `ready`：检查 Tunnel ID、Restricted runtime key 的 Tunnels Read / Use 权限、Tunnel 与工作区的关联，以及本地 runtime 状态。
- ChatGPT 没有显示工具：确认 Developer mode、**Tunnel**、正确的 Tunnel ID、**No authentication** 和本地 `ready` 状态；重启 Gateway 后使用 [Refresh MCP metadata](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)。
- 没有 AoiTalk 工具：确认 AoiTalk 专用 MCP 连接已启用。旧 flag `HOME_MCP_AOITALK_ENABLED` 不影响 catalog。
- Home MCP Gateway 中仍有旧 `aoitalk_*` 名称并返回 `Unknown tool`：在该连接设置中选择“刷新工具”(Refresh)。无需重启 runtime；仅当 Refresh 无法解决时才删除并重新添加连接。
- 曾把凭据粘贴到 ChatGPT：立即轮换 runtime API key，并从对话、应用设置和共享日志中删除。ChatGPT 不需要这个 key。

这是出站 Tunnel，因此排查时不需要添加 Windows 入站防火墙规则。

## 开发测试

设置脚本会运行标准库测试套件，也可以手动运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

如需可选的 pytest 测试套件：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

对真实 Tunnel 或 AoiTalk 服务进行测试时，不要把本地凭据作为测试参数或发送给 ChatGPT。
