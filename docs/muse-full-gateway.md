# Muse から Home MCP Gateway を使う

## 仕組み

`scripts/gateway_muse_http.py` は `home_mcp_gateway.server` が登録した汎用 Gateway の tool registry をそのまま利用し、Streamable HTTP で配信します。ツール実装や登録を複製せず、`scripts/gateway_stdio.py` と同じように checkout を `sys.path` の先頭に置き、汎用プロセスへ不要な管理用 credential 環境変数を引き継ぎません。`HOME_MCP_PROFILE` が設定された起動は拒否します。

HTTP server は `127.0.0.1:8765` だけで待ち受けます。URL path に既存 `path.secret` と同じ秘密が必要で、各 HTTP リクエストには既存 `token.secret` と同じ値の `Authorization: Bearer` header も必要です。秘密本文はファイルから読み、コマンドライン、環境変数、HTTP access log に出しません。tool 監査ログ `muse-full-gateway-audit.jsonl` には tool 名、UTC 時刻、`ok` / `error` のみを JSONL で記録します。

## 公開 URL と ingress

既存 Muse connector の URL 形式を保ちます。

```text
https://nk-h01.f5.si/muse-mcp/<path-secret>/mcp
```

Cloudflare Tunnel の Muse 用 ingress は、秘密 path を含む既存ルールから `http://127.0.0.1:8765` に転送しています。読み取り専用の現状確認で既存ルールと secret の一致を確認しました。公開経路と Muse connector 設定は変更しません。サーバーだけを同じ loopback port で差し替えるため、URL や秘密の再発行は不要です。

## 常駐タスクの導入

リポジトリの `.venv` と既存 `.local/muse-grok-native/mcp/secrets/path.secret` / `token.secret` を使い、既存の `80_HomeMCPGateway - Muse` タスクを置き換えます。起動時に secret ファイルの所有者と ACL を検査します。タスクはログオン時に起動し、hidden PowerShell、Interactive / Limited principal、失敗後の再起動を使います。

```powershell
.\scripts\install_muse_gateway_http_task.ps1
```

```powershell
Get-ScheduledTask -TaskName 80_HomeMCPGateway - Muse | Select-Object TaskName, State
Start-ScheduledTask -TaskName 80_HomeMCPGateway - Muse
Stop-ScheduledTask -TaskName 80_HomeMCPGateway - Muse
```

起動ログは `.local/muse-grok-native/mcp/muse-full-gateway-server.log`、監査ログは同じディレクトリの `muse-full-gateway-audit.jsonl` に保存されます。server は loopback bind のままで、Cloudflare Tunnel が公開転送を担当します。

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_muse_http.py
```

認証なし、誤 token、誤 path が拒否されること、正しい認証で汎用 Gateway と同じ tool 数が返ることを確認します。

## Muse Web 側

既存 connector の URL と Bearer token はそのままです。切り替え後に connector の tool 一覧を refresh（または再接続）すると、汎用 Gateway の tool catalog が表示されます。

Installer は linked worktree からの登録を拒否します。merge 後に main checkout の repository root から実行してください。タスクの絶対パスは worktree 削除後も有効な main checkout を指します。


## Muse リセット後の単一資格情報接続（互換エンドポイント）

Muse の outbound credential proxy は、秘密 URL path と別の Bearer token を同じ HTTP リクエストに付与すると `403 credential_ambiguity` で拒否します。従来の二重認証エンドポイントは一切変更せず、Muse 専用の **Bearer-only** エイリアスを明示的に有効化します。

- 公開 URL: `https://nk-h01.f5.si/muse-bearer/mcp`（秘密でない固定パス）。
- 認証: 従来の `token.secret` と一致する `Authorization: Bearer <TOKEN>` のみ。TOKEN を URL、リポジトリ、コマンドラインに含めない。
- Muse の Custom Connector Credential には `custom.home-mcp-gateway-token` のみを付与する。以前作成した PATH_SECRET Credential は互換エイリアスでは使用しない。
- Windows の `start_muse_gateway_http.ps1` は `--enable-muse-bearer-only` を付与する。サーバー内で厳格に Bearer を照合したあと既存 MCP path に内部 rewrite する（HTTP redirect はしない）。
- 同じ `aoitalk-home` Tunnel に専用 ingress `^/muse-bearer/mcp$` → `http://127.0.0.1:8765` を追加する。管理用スクリプトは `85_CaddyGateway/scripts/Manage-MuseBearerIngress.mjs`。既存 Muse/Claude/portal の ingress は維持する。
- URL path に秘密を置かないため、この経路は Bearer token **1要素** の認証となる。強い token を安全な資格情報ストアに保持し、HTTPS 経由に限定し、トークンの漏洩時は失効・再発行する。
- 認証なし・誤 Bearer は `401`、未登録 path は `404`。tool 実行の監査ログは従来どおり。
- `--enable-muse-bearer-only` を付けない起動、Claude 用 `/claude-mcp` 起動ではこのエイリアスは存在しない。

検証: `python -m pytest tests/test_muse_http.py` で二重認証の後方互換性と Bearer-only エイリアスをテストし、Muse 側では `tools/list` および読み取り専用の `tools/call` だけを実施する。
