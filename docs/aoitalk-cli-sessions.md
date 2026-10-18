# AoiTalk CLI 継続セッション

`HOME_MCP_AOITALK_ENABLED=1` のとき、Gateway は AoiTalk が所有する CLI 継続セッションを8つのMCP toolとして公開します。GatewayはCLI executable、providerのnative session handle、LLM loopを起動せず、認証済みAoiTalk APIへ1リクエストずつ転送します。

公開toolは `aoitalk_cli_session_create`、`send`、`list`、`status`、`collect`、`tail`、`cancel`、`reconnect` です。`create` のproviderは `codex-cli` / `claude-cli` / `grok-cli` / `antigravity-cli`、permission modeは `native_default`（既定）、`standard`、`accept_edits` です。modeとproviderの実行可否は41側のdeployment policyが決定し、Gatewayはfallbackしません。`create` と `send` は必須の `idempotency_key` をヘッダーと本文へ渡します。応答が失われた場合は `submission_uncertain=true` を返し、同じkeyで照合可能な create/send だけを再試行します。`reconnect` は受理不明時に再送せず、status/eventsで確認します。

`tail` は1セッションのopaque cursor以降のbounded event、`collect` は複数セッションのcursor mapを取得します。messageはAPI上限の100,000文字、event取得は1回500件まで、cursor mapは200セッションまでをGateway側でも検証・制限します。出力予算でevent本文を省略した場合は `next_cursor` を進めず、同じcursorの再取得を促します。provider-owned native session id、lease token、credential、環境変数はMCPへ返しません。

認証・session ownership・provider実行・cancel/reconnectの意味は41 AoiTalkが正本です。80側でCLI providerを直接起動したり、AoiTalkの通常CLI session storeを複製したりしません。41のAPIが停止している場合、Gatewayは接続エラーを返し、別providerやstateless実行へfallbackしません。

Gatewayの `HOME_MCP_AOITALK_ENABLED=1` は公開入口だけを有効にします。41側の `cli_development_sessions.enabled`、trusted Windows policy、owner/Agent allow-list、provider cache rootが有効でない場合は、Gatewayはその固定error codeを返し、設定を変更したり別laneへfallbackしたりしません。

通常のAoiTalk連携と、41 AgentRunから起動する80のisolated read-only stdio childは別の経路です。後者は `HOME_MCP_PROFILE=local_agent_read_only`、`HOME_MCP_AOITALK_ENABLED=0` の4 read-only toolだけで、CLI session toolを再公開しません。
