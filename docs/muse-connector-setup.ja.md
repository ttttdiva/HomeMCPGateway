# Muse カスタムコネクター設定・403診断ガイド

Home MCP Gateway の **Muse用MCP接続**を新規作成・別アカウントに追加するときの手順です。認証エラー時の切り分けも含みます。汎用GatewayはWindows上で強い権限を持つため、接続相手と資格情報の管理には注意してください。

## 構成と前提

```text
Museアカウント A ─┐
                  ├─ HTTPS / Cloudflare Tunnel ─> 127.0.0.1:8765 ─> Home MCP Gateway
Museアカウント B ─┘
```

- Windows側では `scripts/start_muse_gateway_http.ps1` とタスク `80_HomeMCPGateway - Muse` を使います。サーバー本体の設定やインストールをアカウントごとに複製する必要はありません。
- Muse側は **MCP（Streamable HTTP）** のカスタムコネクターを追加します。既存構成では、Museのチャットに作成を依頼する方式で設定しました。一般の「設定 → コネクター」一覧から直接追加できない場合はチャットで依頼してください。
- 推奨公開URL形式は **`https://<your-public-host>/muse-bearer/mcp`** です。ご自身のHTTPS ingressがこのパスを `http://127.0.0.1:8765` に転送している必要があります。
- 認証は **Bearer Token 1つだけ**。従来の `/muse-mcp/<path-secret>/mcp` と `PATH_SECRET` を組み合わせた二重資格情報方式を、MuseのBearer専用エイリアスと混ぜないでください。Museの資格情報プロキシから `403 credential_ambiguity` が返ることがあります。
- 既存運用のtokenは `.local/muse-grok-native/mcp/secrets/token.secret` に保存されています。**中身をチャット本文、プロンプト、ソース、Git、ログ、URLに記載しないこと。** Museが提示するセキュアな資格情報入力画面に、ファイルのトークン文字列だけを入れてください（`Bearer ` は付けない）。資格情報名を求められたら既存構成では `custom.home-mcp-gateway-token` を使用します。
- 同一URLとトークンを使う複数のMuseアカウントを、Gatewayは識別しません。A/B両方が同時にPCへアクセスできます。切り替えはMuse側で使用するアカウントを選ぶだけです。古いアカウントを**確実に失効**させるにはトークンのローテーションが必要です。
- SlackコネクターはHomeMCPGatewayと**別系統**です。DOTSとの会話用に同じSlackワークスペースを新アカウントでも接続できますが、2つのMuseが同じ会話へ同時応答する運用には注意してください。

## Museに送るセットアップ依頼文（URLのみ置換して貼り付け）

次のコードブロック全体を**新しいMuseのチャット**に送ります。接続先を自分の公開ホストに置き換えてください。**トークン本文はこの文章に入れません。**

```text
HomeMCPGateway のカスタムコネクターを作成してください。

- 名前：HomeMCPGateway
- 接続方式：MCP（Streamable HTTP）
- URL：https://<your-public-host>/muse-bearer/mcp
- 認証：HTTP Bearer Tokenのみ（資格情報は1種類）
- PATH_SECRET、秘密URLパス、クエリ文字列内のトークン、追加の認証方式は使用しない

認証情報はMuseのSecure Credentials Storeの入力画面で登録します。
私が安全な入力画面でTokenの値だけを入力します。チャット本文にTokenを求めたり表示したりしないでください。
資格情報名が必要なら custom.home-mcp-gateway-token を使い、Bearer という接頭辞は入力値に含めません。

既存のGatewayサーバーのコード、認証設定、トークン、Cloudflare設定は変更しないでください。

接続後、tools/listでMCPツール一覧を取得し、system_infoを読み取り専用の疎通確認として1回実行してください。
403等が起きても、まずレスポンスのHTTPステータス・ヘッダー・本文のエラーコードと返却元を確認してください。
403だけを根拠にToken誤入力と判断したり、入力し直すよう依頼したりしないでください。
Cloudflare Error 1010、Museのcredential_ambiguity、Gateway自身の401を区別して報告してください。
Cloudflareの1010の場合はUser-Agent等のリクエスト特性を確認してください。
機密のAuthorizationヘッダー値やTokenはレポートに含めないでください。
```

## 401 / 403 の切り分け

**重要:** `scripts/gateway_muse_http.py` の認証実装が返すのは、Bearerトークンの不一致・欠落で **401**、対象外パスで **404** です。**403を受けたというだけではトークンの誤りを意味しません。**

| 観測 | 有力な原因 | 対応 |
| --- | --- | --- |
| Gatewayが `401 unauthorized` を返す | Bearer欠落・不一致 | Secure Credentials Storeの対象、接頭辞・空白の扱い、サーバーと入力したTokenの一致を確認 |
| Gatewayが `404 not found` を返す | URLや`/muse-bearer/mcp`エイリアスが未有効 | URL、Cloudflare ingress、`--enable-muse-bearer-only`を確認 |
| Muse側で `403 credential_ambiguity` | Museの資格情報プロキシによる拒否 | 秘密URLパスや追加Credentialを外し、`/muse-bearer/mcp` + Bearerだけにする |
| Cloudflare側で `403` + `Error 1010` | ブラウザーシグネチャ（User-Agent等）によるブロック | レスポンス本文、`cf-ray`、Security Events、HTTPクライアントのUser-Agentを確認。クライアントが設定可能なら識別しやすいUser-Agentで再検証 |
| Cloudflare側のそれ以外の`403` | WAF、アクセスルール等の可能性 | Cloudflareのエラー本文・Ray ID・Security Eventsで根拠を確認 |
| `200`でMCP initializeが成功 | 認証・経路は少なくとも成立 | 続いて`tools/list`と`system_info`を確認 |

診断では、**レスポンス本文とヘッダーを先に見る**ことが重要です。HTTPステータスだけでは、Muse側の資格情報プロキシ、Cloudflare、Gatewayのどこが拒否したのか確定できません。Cloudflareのセキュリティ機能を無条件に全体無効化しないでください。

### 2026年11月の確認事例

- Windowsの`127.0.0.1:8765/muse-bearer/mcp`に対し、正しいBearerは`200`、誤ったBearer・認証なしは`401`。
- 公開HTTPS URLに対して、Python標準`urllib`の既定User-Agentでは`403` + **Cloudflare Error 1010**。説明的なアプリ名のUser-Agentを付けると、**同じBearerのまま`200`**。
- したがって「Museから403」とだけ報告された場合、コピペミスと決めつけず、**実際の403の本文・ヘッダーを収集する**。この事例だけでMuse自身の403原因まで確定したわけではありません。

Cloudflare公式文書は1010を **“access to the website is denied based on your browser's signature.”**（「ブラウザーのシグネチャに基づきサイトへのアクセスが拒否された」）と説明しています。既定のPython `urllib` User-Agentを例示し、アプリを識別できるUser-Agentの利用を案内しています。

- [Cloudflare公式：Error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)
- [Cloudflare公式：403 Forbidden](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-403/)
- [Cloudflare公式：Ray ID](https://developers.cloudflare.com/fundamentals/reference/cloudflare-ray-id/)

## 初回登録時・複数アカウント追加時のチェック

1. `80_HomeMCPGateway - Muse` が起動し、`127.0.0.1:8765` がlistenしていること。
2. 公開HTTPSの `/muse-bearer/mcp` がGatewayへ転送されること。
3. 新Museのカスタムコネクターが**Bearer-only**であること（`PATH_SECRET`なし）。
4. TokenはSecure Credentials Storeでのみ登録したこと（URLやチャットには載せない）。
5. `tools/list` と読み取り専用の `system_info` が成功すること。失敗時は上表に沿って拒否層を特定すること。
6. 複数のMuseを使う場合、Gatewayではどちらのアカウントか区別できないことと、Slackの切り替えは別作業であることを理解すること。

詳細なWindows側の動作と従来エンドポイントについては [Muse Gatewayの仕組み](muse-full-gateway.md) を参照してください。
