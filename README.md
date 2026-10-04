[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

# Home MCP Gateway

Home MCP Gateway は、Windows PC 上で動くローカル MCP サーバーを OpenAI Secure MCP Tunnel 経由で ChatGPT に接続し、PC のファイル・プロセス・ネットワーク・デスクトップ操作を MCP ツールとして提供します。

> **重要な警告:** この Gateway は、実行中の Windows ユーザーが利用できるローカルのファイル、プロセス、ネットワーク、デスクトップに無制限にアクセスできます。信頼できるプライベートトンネルでのみ使用し、公開インターネットや信頼できないユーザーには接続しないでください。

## 必要なもの

- Windows PC
- Python 3.10 以上
- Tunnel と Developer mode を利用できる適格な OpenAI / ChatGPT ワークスペース
- このリポジトリを実行できる PowerShell

## PC 側のセットアップ

以下は、公開リポジトリから取得して初回接続する手順です。

1. リポジトリを取得します。

   ```powershell
   git clone https://github.com/ttttdiva/HomeMCPGateway
   Set-Location HomeMCPGateway
   ```

   Git を使わない場合は、同じ公開リポジトリの [Code → Download ZIP](https://github.com/ttttdiva/HomeMCPGateway) で ZIP をダウンロードし、展開したフォルダーを作業フォルダーにしてください。

2. 依存関係、仮想環境、開発用の基本テストをセットアップします。

   ```powershell
   .\scripts\setup_windows.ps1
   ```

3. [公式 tunnel-client リリース](https://github.com/openai/tunnel-client/releases/latest) から Windows **amd64 または arm64 の完全な ZIP** をダウンロードします。runtime-only の ZIP は使わないでください。展開した `tunnel-client.exe` を PATH に置くか、このリポジトリ内の展開フォルダーに置くか、`.env` の `TUNNEL_CLIENT_PATH` にフルパスを指定します。必要なら同じリリースの `SHA256SUMS.txt` でハッシュを確認してください。

4. [OpenAI Organization の Tunnels 設定](https://platform.openai.com/settings/organization/tunnels) を開き、Tunnel を作成または既存の Tunnel を確認し、使用する ChatGPT ワークスペースに関連付けます。ここで扱う値を混同しないでください。

   - **Tunnel ID** は接続先を識別する値です。秘密ではなく、`.env` の `CONTROL_PLANE_TUNNEL_ID` に入れます。
   - **Runtime API key** は、この PC の tunnel-client が実行時に使う Restricted API key です。次の手順で作成し、`.env` の `CONTROL_PLANE_API_KEY` にだけ保存します。
   - **Admin key** は Organization の Tunnel を作成・変更・確認する管理用の認証情報です。Runtime API key の代わりにはならず、`.env` や ChatGPT に貼り付けません。

5. [Organization の API keys](https://platform.openai.com/settings/organization/api-keys) で **Restricted** runtime API key を作成し、**Tunnels: Read** と **Tunnels: Use** だけを付与します。キーは作成時に一度だけ表示されるため、その場で安全に保管してください。管理用の認証情報と runtime API key を取り違えないでください。

6. サンプルをコピーしてプレースホルダーを埋めます。

   ```powershell
   Copy-Item .env.example .env
   ```

   `.env` の最低限の値は次のとおりです。`TUNNEL_CLIENT_PATH` は PATH や標準のリポジトリ内検索で見つからない場合だけ設定します。

   ```dotenv
   CONTROL_PLANE_TUNNEL_ID=<your-tunnel-id>
   CONTROL_PLANE_API_KEY=<your-restricted-runtime-api-key>
   TUNNEL_ALIAS=home-mcp
   # TUNNEL_CLIENT_PATH=<full-path-to-tunnel-client.exe>
   ```

   `.env`、Tunnel ID、runtime API key は Git に追加せず、ChatGPT のメッセージやアプリ設定にも入力しないでください。

7. Gateway を接続します。

   ```powershell
   .\scripts\connect_tunnel.ps1
   ```

   スクリプトは `home-mcp` というローカル runtime を起動し、`process_running`、`healthy`、`ready` を確認します。接続は外向きに行われるため、受信用のファイアウォールポートを開ける必要はありません。

## ChatGPT 側のセットアップ

1. ChatGPT の Developer mode を有効にします。
2. アプリ / connector の作成画面を開き、接続方式に **Tunnel** を選びます。
3. 先ほど作成または確認した Tunnel を選択するか、Tunnel ID を貼り付けます。認証方式は **No authentication** を選びます。runtime API key は tunnel-client と OpenAI の間だけで使われ、ChatGPT に渡しません。
4. 表示されたツールを確認してアプリ / connector を作成し、Gateway の runtime が `ready` になっている状態で接続します。

公式の手順は [Connect ChatGPT to a remote MCP server](https://developers.openai.com/plugins/deploy/connect-chatgpt)、メタデータ更新は [Refresh MCP metadata](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata) を参照してください。ChatGPT のプロジェクトや接続アプリの一般的な使い方は [Using Projects in ChatGPT](https://help.openai.com/en/articles/10169521-using-projects-in-chatgpt) にあります。

## 起動・自動起動・状態確認・停止

- 通常起動: `.\scripts\connect_tunnel.ps1`
- 監視と再接続: `.\scripts\connect_tunnel.ps1 -Watch`
- 状態確認: `tunnel-client.exe runtimes status home-mcp --json`
- 停止: `tunnel-client.exe runtimes stop home-mcp`

`tunnel-client.exe` が PATH にない場合は、コマンドの実行ファイル部分を展開先のフルパスに置き換えるか、`.env` の `TUNNEL_CLIENT_PATH` を設定してください。`connect_tunnel.ps1 -Watch` は状態を定期確認し、異常時に再接続します。

自動起動と異常時の再接続を有効にするには、リポジトリのルートで次を実行します。このコマンドは現在の Windows ユーザー用に `Home MCP Gateway` タスクをインストールし、直ちに起動します。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

自動起動を止めるときは、先に `Home MCP Gateway` タスクを無効化または停止してから runtime を停止します。

```powershell
Disable-ScheduledTask -TaskName 'Home MCP Gateway'
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
tunnel-client.exe runtimes stop home-mcp
```

再開する場合はタスクを有効化して `connect_tunnel.ps1 -Watch` を起動します。`.env` を変更した後は runtime を停止してから `connect_tunnel.ps1` を再実行し、ChatGPT 側でメタデータを更新してください。

## AoiTalk（任意）

AoiTalk 連携は初期状態では無効です。有効にする場合は、ローカルの `.env` に次を追加し、runtime を再起動して ChatGPT の接続を更新または Refresh します。

```dotenv
HOME_MCP_AOITALK_ENABLED=1
```

AoiTalk 自体が PC にインストール済みで、接続先・認証・必要な機能が設定済みである必要があります。この Gateway は AoiTalk をインストールまたは設定しません。AoiTalk が無効なら **76 tools**、有効なら **84 tools** が公開されます。認証情報を ChatGPT に渡さないでください。

## ツールの概要

標準構成は 76 ツールです。主なグループは次のとおりです。

| グループ | 主な内容 |
| --- | --- |
| ホスト・ファイル | システム情報、環境変数、ファイルの読み書き、検索、コピー、移動、削除、ハッシュ |
| コマンド・プロセス | 任意の shell / PowerShell / Python、プロセスの開始・確認・終了 |
| ネットワーク | 任意の HTTP(S) リクエスト、ダウンロード、TCP / HTTP の readiness 確認 |
| デスクトップ・端末 | Windows の観測・UI 操作、スクリーンショット（MCPネイティブPNG）、座標系 `screen`/`window`/`normalized` による視覚付き Computer Use（[詳細](docs/computer-use.ja.md)）、Android / ADB |
| 開発ワークフロー | Git リポジトリ / worktree、長時間ジョブ、ブラウザ（Playwright） |
| 画像・診断 | 画像コンテキスト、ツール実行のローカル診断ログ |
| AoiTalk（任意） | クリップ取り込みとローカルタスク委譲。上記の 8 ツールを追加 |

これらのグループにもアクセス制限や read-only 境界が追加されるわけではありません。接続相手とプロンプトを信頼できる範囲に限定してください。

## トラブルシューティング

- `Python 3.10+ was not found` が出る場合: Python 3.10 以上をインストールし、PowerShell を開き直して `setup_windows.ps1` を再実行します。
- `tunnel-client.exe` が見つからない場合: runtime-only ではない完全な Windows ZIP を展開し、`tunnel-client.exe` を PATH に置くか `TUNNEL_CLIENT_PATH` を設定します。
- runtime が `ready` にならない場合: Tunnel ID、Restricted runtime API key の Tunnels Read / Use、Tunnel と ChatGPT ワークスペースの関連付け、ローカルの runtime 状態を確認します。
- ChatGPT にツールが表示されない場合: Developer mode、アプリの **Tunnel**、正しい Tunnel ID、**No authentication**、runtime の `ready` を確認してから、Gateway を再起動し [Refresh MCP metadata](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata) を実行します。
- AoiTalk のツールが表示されない場合: `.env` に `HOME_MCP_AOITALK_ENABLED=1` を設定して runtime を再起動し、AoiTalk 自体のインストール・設定・接続状態を確認してから refresh します。
- 認証情報を誤って貼り付けた場合: runtime API key を直ちにローテーションし、ChatGPT の会話・アプリ設定・共有ログから削除します。ChatGPT は runtime API key を必要としません。

受信用ポートを開ける方式ではないため、Tunnel が接続できないときに Windows の受信ファイアウォール規則を追加する必要はありません。

## 開発者向けテスト

セットアップ スクリプトが実行する基本テスト:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

開発用 extra をインストールして pytest も実行できます。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

実際の Tunnel や AoiTalk に接続するテストでは、ローカルの資格情報をテスト引数や ChatGPT に渡さないでください。
