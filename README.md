[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

# Home MCP Gateway

**クラウドの AI（ChatGPT / Claude Code・claude.ai / Muse）から、Windows ワークスペースを MCP ツールとして操作するためのトンネル MCP サーバーです。**（自宅の PC を操作する用途にも使えます）

PC 側では同じ Gateway（92 ツール）を動かし、各 AI サービスにはそれぞれのコネクタ（プラグイン）として登録します。受信ポートを開ける必要はありません。

```text
ChatGPT        ── OpenAI Secure MCP Tunnel ──┐
Claude Code ─┐                               ├─> Home MCP Gateway ─> Windows
claude.ai   ─┴─ HTTPS (Cloudflare Tunnel 等) ─┤   (ファイル / コマンド / ブラウザ /
Muse           ── HTTPS (Cloudflare Tunnel 等) ─┘    デスクトップ / Git / Android ...)
```

> **警告:** Gateway は実行中の Windows ユーザーと同じ権限で、ファイル・プロセス・ネットワーク・デスクトップを**無制限に**操作できます。自分だけが使うプライベートな接続に限定し、URL やトークンは絶対に共有しないでください。

## できること

チャットで頼むだけで、AI が Gateway を動かしている Windows 上で次のような作業をします。

| 分類 | 例 |
| --- | --- |
| ファイル | 読み書き、検索、コピー・移動・削除、ハッシュ |
| コマンド・プロセス | PowerShell / shell / Python の実行、プロセスの起動・監視・終了 |
| 長時間ジョブ | Codex などの CLI エージェントをバックグラウンドで走らせて進捗確認・指示追加 |
| 開発 | Git の状態確認、worktree 作成・差分・後片付け |
| ブラウザ | Playwright 操作、ログイン済み Edge へのアタッチ（[ガイド](docs/browser-edge-attach.ja.md)） |
| デスクトップ | スクリーンショット、画面観測とクリック・入力などの Computer Use（[詳細](docs/computer-use.ja.md)） |
| ネットワーク | HTTP リクエスト、ダウンロード、ポート / HTTP の起動待ち |
| Android | ADB 経由のタップ、入力、スクリーンショット、アプリ管理 |

## 共通の準備

Windows と Python 3.10 以上が必要です。

```powershell
git clone https://github.com/ttttdiva/HomeMCPGateway
Set-Location HomeMCPGateway
.\scripts\setup_windows.ps1
```

あとは使いたい AI サービスの分だけ、以下を設定します。どれも常駐タスクとして自動起動します。

## ChatGPT

OpenAI の Secure MCP Tunnel を使います（Developer mode が使えるワークスペースが必要）。

1. [tunnel-client](https://github.com/openai/tunnel-client/releases/latest) の Windows 用**完全版 ZIP** を展開し、リポジトリ内に置くか `.env` の `TUNNEL_CLIENT_PATH` で指定します。
2. [OpenAI の Tunnels 設定](https://platform.openai.com/settings/organization/tunnels)で Tunnel を作り ChatGPT ワークスペースに関連付け、[API keys](https://platform.openai.com/settings/organization/api-keys) で **Tunnels: Read / Use** だけの Restricted key を作ります。
3. `.env` を作成します。

   ```powershell
   Copy-Item .env.example .env   # CONTROL_PLANE_TUNNEL_ID と CONTROL_PLANE_API_KEY を記入
   .\scripts\install_autostart.ps1   # タスク「80_HomeMCPGateway - ChatGPT」を登録・起動
   ```

4. ChatGPT の Developer mode でアプリを作成し、接続方式 **Tunnel**、認証 **No authentication** を選びます。API key は ChatGPT に入力しません。

手動で一度だけ接続するなら `.\scripts\connect_tunnel.ps1`、再起動は `Restart-Gateway.bat` です。

## Claude Code（クラウド）/ claude.ai

ローカルの `127.0.0.1:8766` で HTTP の MCP サーバーを動かし、自前の HTTPS 経路（Cloudflare Tunnel など）で公開します。

```powershell
.\scripts\install_claude_gateway_http_task.ps1   # タスク「80_HomeMCPGateway - Claude Code」
```

初回に秘密 URL パスと Bearer トークンが `.local/claude-code-mcp/secrets/` に生成されます。`/claude-mcp/<path-secret>/mcp` を 8766 へ転送する ingress を用意し、Claude の **Customize > Connectors > Custom** に `connector.txt` の URL と `Authorization: Bearer ...` ヘッダーを登録します。詳細は [Claude Code 接続](docs/claude-code-remote-mcp.md)。

## Muse

Muse のカスタムコネクタに Gateway を直接持たせます。ローカルの `127.0.0.1:8765` で動かし、HTTPS 経路で公開します。

```powershell
.\scripts\install_muse_gateway_http_task.ps1   # タスク「80_HomeMCPGateway - Muse」
```

`.local/muse-grok-native/mcp/secrets/` に `path.secret` / `token.secret` を用意しておく必要があります。Muse の資格情報プロキシ向けに、Bearer トークンだけで認証する `/muse-bearer/mcp` も使えます。詳細は [Muse 接続](docs/muse-full-gateway.md)。

## 常駐タスク

| タスク名 | 接続先 | ローカル |
| --- | --- | --- |
| `80_HomeMCPGateway - ChatGPT` | ChatGPT | tunnel-client |
| `80_HomeMCPGateway - Claude Code` | Claude Code / claude.ai | `127.0.0.1:8766` |
| `80_HomeMCPGateway - Muse` | Muse | `127.0.0.1:8765` |

コードや `.env` を変えたら該当タスクを `Stop-ScheduledTask` → `Start-ScheduledTask` で再起動し、AI 側でツール一覧を更新（Refresh）してください。

## AoiTalk 連携（任意）

AoiTalk を使っている場合のみ、専用 MCP（35 ツール）を別の Tunnel で公開できます（`connect_tunnel.ps1 -Target aoitalk`）。汎用 Gateway には含まれません。詳細は [AoiTalk 専用 MCP](docs/aoitalk-plugin.md)。

## その他のドキュメント

- [Codex 起動と WindowsApps の扱い](docs/codex-app-server.md) — Gateway から起動する Codex はフルアクセス・環境変数全継承で動きます
- [長時間ジョブの dispatcher / supervisor](docs/work-dispatcher.md)、[resource queue](docs/resource-queue.md)
- [Muse Workspace MCP](docs/muse-workspace-mcp.md) — 登録した worktree だけを Muse に見せる別サーバー

## トラブルシューティング

- **ツールが表示されない / 古い:** タスクが動いているか確認し、AI 側のコネクタでツール一覧を Refresh します。
- **ChatGPT で `ready` にならない:** Tunnel ID、Restricted key の権限（Tunnels Read / Use）、Tunnel とワークスペースの関連付けを確認します。
- **秘密情報を貼ってしまった:** 該当のキー / トークンをすぐ再発行し、会話やログから削除してください。

## テスト

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```
