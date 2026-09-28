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

7. 启动连接。

   ```powershell
   .\scripts\connect_tunnel.ps1
   ```

   此脚本会启动名为 `home-mcp` 的本地 runtime，并检查 `process_running`、`healthy` 和 `ready`。连接是出站连接，不需要开放入站防火墙端口。

## ChatGPT 端设置

1. 在 ChatGPT 中启用 **Developer mode**。
2. 打开应用 / connector 创建流程，选择 **Tunnel** 作为连接类型。
3. 选择刚刚创建或查看的 Tunnel，或粘贴 Tunnel ID。选择 **No authentication**。Runtime API key 只在本地 tunnel-client 与 OpenAI 之间使用，ChatGPT 永远不会收到它。
4. 检查显示的工具，创建应用 / connector，并在本地 runtime 报告 `ready` 时连接。

请参阅官方 [Connect ChatGPT to a remote MCP server 指南](https://developers.openai.com/plugins/deploy/connect-chatgpt)、[Refresh MCP metadata 说明](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)以及 [Using Projects in ChatGPT 帮助文章](https://help.openai.com/en/articles/10169521-using-projects-in-chatgpt)。

## 启动、自动启动、状态和停止

- 普通启动：`.\scripts\connect_tunnel.ps1`
- 监视并重连：`.\scripts\connect_tunnel.ps1 -Watch`
- 查看状态：`tunnel-client.exe runtimes status home-mcp --json`
- 停止 runtime：`tunnel-client.exe runtimes stop home-mcp`

如果 `tunnel-client.exe` 不在 PATH 中，请将命令中的可执行文件名替换为解压后的完整路径，或在 `.env` 中设置 `TUNNEL_CLIENT_PATH`。`-Watch` 模式会定期检查并重连不健康的 runtime。

要启用自动启动和异常重连，请在仓库根目录运行以下命令。它会为当前 Windows 用户安装并立即启动名为 `Home MCP Gateway` 的计划任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

不要把凭据写入任务参数。停止自动启动的 runtime 时，先禁用或停止 `Home MCP Gateway` 任务，再停止 runtime：

```powershell
Disable-ScheduledTask -TaskName 'Home MCP Gateway'
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
tunnel-client.exe runtimes stop home-mcp
```

恢复时重新启用任务并启动 `connect_tunnel.ps1 -Watch`。修改 `.env` 后，先停止 runtime，再重新运行 `connect_tunnel.ps1`，并刷新 ChatGPT 的连接元数据。

## AoiTalk（可选）

AoiTalk 集成默认关闭。要启用它，请在本地 `.env` 中添加以下内容，重启 runtime，然后刷新 ChatGPT 连接：

```dotenv
HOME_MCP_AOITALK_ENABLED=1
```

AoiTalk 本身必须已经安装并在本机配置好，包括端点、认证和所需功能。本 Gateway 不会安装或配置 AoiTalk。默认配置公开 **76 个工具**；启用 AoiTalk 后公开 **84 个工具**。不要将 AoiTalk 凭据发送给 ChatGPT。

## 工具分组

默认配置公开 76 个工具，主要分组如下：

| 分组 | 示例 |
| --- | --- |
| 主机与文件 | 系统信息、环境变量、文件读写、搜索、复制、移动、删除、哈希 |
| 命令与进程 | 任意 shell / PowerShell / Python、进程启动、检查和终止 |
| 网络 | 任意 HTTP(S) 请求、下载以及 TCP / HTTP readiness 检查 |
| 桌面与设备 | Windows 观测/UI 操作、截图以及 Android / ADB |
| 开发工作流 | Git 仓库 / worktree、持久化任务以及浏览器会话（Playwright） |
| 图像与诊断 | 图像上下文和本地工具计时诊断 |
| AoiTalk（可选） | 剪辑导入和本地任务委派；增加 8 个工具 |

这些分组不会额外建立 allowlist 或 read-only 边界。请只将连接和提示交给可信的人和工作流。

## 故障排查

- 出现 `Python 3.10+ was not found`：安装 Python 3.10 或更高版本，重新打开 PowerShell，再次运行 `setup_windows.ps1`。
- 找不到 `tunnel-client.exe`：解压完整 Windows ZIP（不是 runtime-only 包），将可执行文件放入 PATH，或设置 `TUNNEL_CLIENT_PATH`。
- runtime 没有变成 `ready`：检查 Tunnel ID、Restricted runtime key 的 Tunnels Read / Use 权限、Tunnel 与工作区的关联，以及本地 runtime 状态。
- ChatGPT 没有显示工具：确认 Developer mode、**Tunnel**、正确的 Tunnel ID、**No authentication** 和本地 `ready` 状态；重启 Gateway 后使用 [Refresh MCP metadata](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)。
- 没有 AoiTalk 工具：设置 `HOME_MCP_AOITALK_ENABLED=1`，重启 runtime，确认 AoiTalk 已安装并配置，然后刷新连接。
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
