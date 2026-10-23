# AoiTalk CLI 継続セッション

`HOME_MCP_AOITALK_ENABLED=1` のとき、Gateway は AoiTalk が所有する CLI 継続セッションを8つのMCP toolとして公開します。GatewayはCLI executable、providerのnative session handle、LLM loopを起動せず、認証済みAoiTalk APIへ1リクエストずつ転送します。

公開toolは `aoitalk_cli_session_create`、`send`、`list`、`status`、`collect`、`tail`、`cancel`、`reconnect` です。`create` のlocal providerは `codex-cli` / `claude-cli` / `grok-cli` / `antigravity-cli`、permission modeは `native_default`（既定）、`standard`、`accept_edits` です。modeとproviderの実行可否は41側のdeployment policyが決定し、Gatewayはfallbackしません。`create` と `send` は必須の `idempotency_key` をヘッダーと本文へ渡します。応答が失われた場合は `submission_uncertain=true` を返し、同じkeyで照合可能な create/send だけを再試行します。`reconnect` は受理不明時に再送せず、status/eventsで確認します。

`muse-remote` は41側のMuse remote routeを識別するproviderです。Gatewayのremote `create` は `agent_id`、`provider`、`idempotency_key` と、指定された `agent_revision_id` / `title` / `execution_profile_id` / `role_context_mode` だけを転送します。`agent_revision_id` はlocal/remote双方で任意の明示pinとして受け付け、UUID規約で検証後その値をそのまま送ります。未指定または `null` は本文から省略します。`role_context_mode` は `raw` または `agent_role` を指定でき、未指定または `null` なら本文から省略して41側の既定値に任せます。`agent_role` でrevisionを省略した場合は、41側が正規権限を検証したrevisionをatomicに固定します。Gatewayはlatest revisionを照会・注入・再送しません。明示pinがstaleなら41側の `agent_revision_stale` を保持します。`model`、`effort`、`working_directory`、`permission_mode`、`allowed_capabilities` はremote routeで使わないため、値がある場合も明示的な `null` の場合も、HTTP送信前に `invalid_input` を返します。Gatewayはこれらの代替値や仮のrouteを生成しません。local providerでは従来どおり `model` と `working_directory` が必須で、省略したpermission modeは `native_default`、capabilityの指定は指定時だけ本文へ渡します。

Museのremote target、認証・capability policy、provider route、role deliveryは41 AoiTalkが所有します。remote routeはlocal filesystem、cwd、model、effort、command execution、workspace read/writeを制御せず、role deliveryも41側の契約に従います。Gatewayはそのroleやpolicyを再実装せず、受け付けた共通引数と任意の `title` / `execution_profile_id` / `role_context_mode` を認証済みAPIへ転送します。

remoteの成功応答・session projectionには実行境界を明示する `execution_target=remote_muse`、`local_filesystem=false`、`sandbox=remote_uncontrolled` を付けます。実際に返された `agent_revision_id` / `binding_id` / `role_context_mode` / `role_context_sha256`、`turn_status`、安全なrole `delivery` の状態を値どおり返します。prepared/sent状態をrole適用やモデル遵守の証明として読み替えません。Gatewayは資格情報、lease token、role本文、remote projectionに含まれるworking directoryやlocal pathをMCP出力へ渡しません。

このsource同期は契約とMCP client/schemaまでです。今回live API、provider、Tunnelへ接続しておらず、実環境routeの有効化は確認していません。

`tail` は1セッションのopaque cursor以降のbounded event、`collect` は複数セッションのcursor mapを取得します。messageはAPI上限の100,000文字、event取得は1回500件まで、cursor mapは200セッションまでをGateway側でも検証・制限します。出力予算でevent本文を省略した場合は `next_cursor` を進めず、同じcursorの再取得を促します。provider-owned native session id、lease token、credential、環境変数はMCPへ返しません。

認証・session ownership・provider実行・cancel/reconnectの意味は41 AoiTalkが正本です。80側でCLI providerを直接起動したり、AoiTalkの通常CLI session storeを複製したりしません。41のAPIが停止している場合、Gatewayは接続エラーを返し、別providerやstateless実行へfallbackしません。

Gatewayの `HOME_MCP_AOITALK_ENABLED=1` は公開入口だけを有効にします。41側の `cli_development_sessions.enabled`、trusted Windows policy、owner/Agent allow-list、provider cache rootが有効でない場合は、Gatewayはその固定error codeを返し、設定を変更したり別laneへfallbackしたりしません。

通常のAoiTalk連携と、41 AgentRunから起動する80のisolated read-only stdio childは別の経路です。後者は `HOME_MCP_PROFILE=local_agent_read_only`、`HOME_MCP_AOITALK_ENABLED=0` の4 read-only toolだけで、CLI session toolを再公開しません。
