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

7. 初回の接続とprofile作成を確認します。トンネル本体を可視コンソールで動かす手順は後述します。

   ```powershell
   .\scripts\connect_tunnel.ps1
   ```

   スクリプトは `home-mcp` というローカル runtime を起動し、`process_running`、`healthy`、`ready` を確認してプロンプトへ戻ります。トンネル画面は自動で開きません。接続は外向きに行われるため、受信用のファイアウォールポートを開ける必要はありません。

## ChatGPT 側のセットアップ

1. ChatGPT の Developer mode を有効にします。
2. アプリ / connector の作成画面を開き、接続方式に **Tunnel** を選びます。
3. 先ほど作成または確認した Tunnel を選択するか、Tunnel ID を貼り付けます。認証方式は **No authentication** を選びます。runtime API key は tunnel-client と OpenAI の間だけで使われ、ChatGPT に渡しません。
4. 表示されたツールを確認してアプリ / connector を作成し、Gateway の runtime が `ready` になっている状態で接続します。

公式の手順は [Connect ChatGPT to a remote MCP server](https://developers.openai.com/plugins/deploy/connect-chatgpt)、メタデータ更新は [Refresh MCP metadata](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata) を参照してください。ChatGPT のプロジェクトや接続アプリの一般的な使い方は [Using Projects in ChatGPT](https://help.openai.com/en/articles/10169521-using-projects-in-chatgpt) にあります。

## 通常起動・再起動・画面表示

### トンネル本体を可視コンソールで実行する

**本体を手元のPowerShellで動かす場合は `tunnel-client.exe run` を使います。** `runtimes connect` は本体を管理対象の別プロセスとして起動・再利用するコマンドです。`connect_tunnel.ps1 -Watch` が表示するのは監視役の画面で、本体の前景コンソールではありません。

以下は、初回接続で `.runtime\profiles\home-mcp.yaml` が作成済みの場合の手順です。先に既存の監視と同じトンネルのruntimeを停止し、二重起動しない状態にしてから、リポジトリのルートで開いた専用PowerShellに貼り付けます。停止手順は下のバックグラウンド運用の節を参照してください。

既存profileのAPI keyは `env:CONTROL_PLANE_API_KEY` 参照です。`run` 自体はリポジトリの `.env` を読み込みません。また、`connect_tunnel.ps1` は終了時に呼出元の環境を元へ戻すため、その実行後もキーがPowerShellに残るとは限りません。次の例は既存スクリプトと同じく `.env` を文字列として読み、キーを表示・コマンド引数へ展開せず、終了時に環境を復元します。

配置先は新しく開いたPowerShell自身で指定します。`$GatewayRoot`の1行を実際の絶対パスへ変更してください。

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

このPowerShellが本体のコンソールです。既存profileはログをファイルへ出すため、`--log.file=`の空値指定で標準出力へ切り替えます。`--log.file stdout`は`stdout`というファイルを作るため使いません。Ctrl+Cで前景実行を終了し、再起動時は同じ手順で `.env` を読み直します。キーの実値を引数に追加する必要はありません。別のprofile名・認証環境変数を使っている場合は、既存profileの参照に合わせてください。

`run` は `runtimes` 管理起動ではないため、管理側の過去の `stopped` 表示や `.runtime\supervisor-status.json` を前景プロセスの生存状態と同一視しません。本体のコンソールとhealth URLで確認してください。ブラウザ管理画面はhealth URLの `/ui` です。最新health URLの保存先はprofileの `health.url_file` で確認できます。

### 非表示のバックグラウンド運用

本体の可視コンソールが不要で、自動起動を設定済みなら、タスクスケジューラの `Home MCP Gateway` タスクを使います。タスクスケジューラで「実行」を選ぶか、次を実行します。

```powershell
Start-ScheduledTask -TaskName 'Home MCP Gateway'
```

このタスクの標準Actionは `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "<repository>\scripts\connect_tunnel.ps1" -Watch` です。非表示で30秒ごとに監視し、異常時に再接続します。タスクが未登録の場合だけ、リポジトリのルートで次を実行して登録します。登録処理はタスクを直ちに起動します。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

起動方式と画面の違いは次のとおりです。

| 入口 | 動作と表示 |
| --- | --- |
| `tunnel-client.exe run --profile-dir ... --profile home-mcp --log.file=` | トンネル本体を現在のコンソールで前景実行。上の環境読込が必要 |
| 登録済み `Home MCP Gateway` タスク | `connect_tunnel.ps1 -Watch` を非表示で継続実行 |
| `.\scripts\connect_tunnel.ps1` | process modeで接続・状態確認後、プロンプトへ戻る。runtimeは継続する |
| `.\scripts\connect_tunnel.ps1 -Watch` | 実行したPowerShell画面で監視役を継続。本体は管理対象の別プロセス |
| `.\scripts\run_gateway.cmd` | stdio Gateway本体の起動。トンネル接続や管理画面を開くバッチではない |

見える専用トンネルウィンドウを開くバッチは、このリポジトリにはありません。`Connected ... using process mode` と `process_running / healthy / ready = True` は、バックグラウンド接続できた状態です。本体を可視起動したという意味ではありません。`connect_tunnel.ps1` の管理画面URLは出力の `ui_url` と `.runtime\supervisor-status.json` に記録されます。Watch画面を閉じることと、管理対象runtimeの停止は別です。

### バックグラウンド運用の状態確認と再起動

以下はリポジトリのルートのPowerShellで実行します。PATHを前提にせず、展開済みの実行ファイルを選びます。`.env` の `TUNNEL_CLIENT_PATH` で別の配置先を指定している場合は、同じフルパスを `$TunnelClient` に設定してください。`.env` の値はPowerShell変数へ自動では読み込まれません。

```powershell
$TunnelClient = (Get-ChildItem -Path '.\tunnel-client-*\tunnel-client.exe' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
if (-not $TunnelClient) { throw '展開先のtunnel-client.exeのフルパスを $TunnelClient に指定してください。' }
& $TunnelClient runtimes status home-mcp --json
```

**`.env`やGatewayコードを変更した後は、接続コマンドの再実行だけでなくruntimeを停止して起動し直します。** 登録済みタスクを使う場合は、監視による再接続を止めてから次の順で操作します（`$TunnelClient` は上で確認した実行ファイルです）。

```powershell
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
& $TunnelClient runtimes stop home-mcp
Start-ScheduledTask -TaskName 'Home MCP Gateway'
```

起動は非同期です。状態確認で3項目がTrueになることを確認し、ChatGPT側でメタデータを更新してください。手動Watchを使っている場合は先にその監視を終了し、runtime停止後に `.\scripts\connect_tunnel.ps1 -Watch` で再開します。`TUNNEL_ALIAS`を変更している場合は、例の `home-mcp` を実際のaliasに置き換えます。

自動起動も止める場合は、タスクを無効化・停止してからruntimeを停止します。再開時はタスクを有効化して実行してください。

```powershell
Disable-ScheduledTask -TaskName 'Home MCP Gateway'
Stop-ScheduledTask -TaskName 'Home MCP Gateway'
& $TunnelClient runtimes stop home-mcp
```

## AoiTalk（任意）

AoiTalk 連携は初期状態では無効です。有効にする場合は、ローカルの `.env` に次を追加し、runtime を再起動して ChatGPT の接続を更新または Refresh します。

```dotenv
HOME_MCP_AOITALK_ENABLED=1
```

AoiTalk 自体が PC にインストール済みで、接続先・認証・必要な機能が設定済みである必要があります。この Gateway は AoiTalk をインストールまたは設定しません。AoiTalk が無効なら **76 tools**、有効なら **92 tools** が公開されます。認証情報を ChatGPT に渡さないでください。

CLI継続セッションの接続と動作は [AoiTalk CLI 継続セッション](docs/aoitalk-cli-sessions.md) を参照してください。

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
| AoiTalk（任意） | クリップ取り込み、ローカルタスク委譲、CLI継続セッション。上記の 16 ツールを追加 |

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
