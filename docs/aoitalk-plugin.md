# AoiTalk専用MCP

## 移行段階

専用MCPは既存のAoiTalk client、認証所有者、schema wrapperを共有し、35 toolsをstdioで公開します。内訳は既存Clip 3、local task 5、API Agent 5、CLI session 8、Task/Docs/Knowledge管理14です。AoiTalk側の独立公開の正本は `41/plugins/AoiTalk_Plugin` です。

この80リポジトリにもstdio entry script `scripts/aoitalk_plugin_stdio.py` と、35 toolsを登録する実装 `home_mcp_gateway/aoitalk_plugin_server.py` があります。entry scriptはこのserver moduleを起動します。現在のlive接続が41側Pluginと80側entryのどちらから配信されているかは未確認で、別作業です。

Phase BのSOURCE変更では、汎用Gatewayから旧21個の `aoitalk_*` 登録と、それだけに使うimport・認証lifespan・設定gateを撤去しました。`HOME_MCP_AOITALK_ENABLED` は汎用catalogを変更しません。汎用78 tools（Codex用job tools 8個を含む）、共有client/schema/library、専用serverは維持します。この変更は稼働中Gateway、AoiTalk、Tunnel、認証設定を変更せず、live catalogへの反映は別作業です。

変更前HEAD `9350ec4` の実catalogから撤去exact names、汎用集合、専用schema/annotations契約を `tests/fixtures/phase_b_catalog_contract.json` に保存しています。offline検証では旧flagの有無に関係なく汎用catalogが同一で、AoiTalk module importを拒否してもgeneric importと正常・例外両方のlifespan終了が成功することを確認します。

専用entry `scripts/aoitalk_plugin_stdio.py` は `home_mcp_gateway.aoitalk_plugin_server` を起動し、`home_mcp_gateway.server` をimportせず、generic GatewayのOS tool、job、telemetry catalogを登録しません。`HOME_MCP_PROFILE` が設定されている場合は起動を拒否します。control-plane/admin keyと専用runtime keyはMCP childへ渡しません。

## SOURCE撤去境界

撤去したexact namesは上記fixtureの `removed_generic_names` に列挙しています。変更前の実登録元は次の4経路です。

| 登録元 | tools数 | genericから外したschema wrapper依存 |
| --- | ---: | --- |
| `aoitalk.TOOLS` | 3 | なし |
| `aoitalk_tasks.TOOLS` | 5 | `aoitalk_task_schema.structured_task_tool` |
| `aoitalk_agent_tasks.TOOLS` | 5 | `aoitalk_agent_task_schema.structured_agent_task_tool` |
| `aoitalk_cli_sessions.TOOLS` | 8 | `aoitalk_cli_session_schema.structured_cli_session_tool` |

genericの `server -> _aoitalk_enabled -> dotenv_values(.env)` と `lifespan -> aoitalk_auth.activate/deactivate` も撤去しました。専用serverの認証lifespanと上記client/schema modulesは保持しています。Codexの `jobs -> work_dispatcher_codex`、dispatcher/supervisor、および実行時validatorから共有AoiTalk libraryを利用する経路も変更していません。

## Tunnelとlauncher

汎用Gatewayは従来どおり `connect_tunnel.ps1` の既定target `home-mcp`、`.runtime/profiles/<alias>.yaml`、`.runtime/supervisor-status.json` を使います。AoiTalkは別Tunnel IDを `HOME_MCP_AOITALK_TUNNEL_ID` に必須設定し、alias/profile `aoitalk`、profile file `.runtime/profiles/aoitalk.yaml`、status/log `.runtime/aoitalk/supervisor-status.json` と `.runtime/aoitalk/supervisor.log`、tool log `.runtime/aoitalk/tool_logs` を使います。AoiTalk IDをhome IDに流用したり、alias/profileを交差させたりするとlauncherが拒否します。

runtime keyは `.env` の `HOME_MCP_AOITALK_RUNTIME_API_KEY` を使えます。未設定の場合は既存 `CONTROL_PLANE_API_KEY` を共有します。admin keyをruntime keyとして使わないでください。

別Tunnelの運用preflightが完了した後に、AoiTalk targetを起動します。

```powershell
.\scripts\connect_tunnel.ps1 -Target aoitalk
```

自動起動は既定TaskName `Home MCP Gateway - AoiTalk` を使います。TaskNameが既存なら、同じrepositoryとtargetのactionであることを確認してから更新します。

```powershell
.\scripts\install_autostart.ps1 -Target aoitalk
```

これらのコマンドはTunnelの新規発行・associationを行いません。live preflight、実runtimeの起動、Plugin GUI導入、Refreshは別作業です。

## Task operations

Task catalog/list/readはAoiTalk APIの専用契約を使います。listはサーバー発行のcompact pageと `X-AoiTalk-Has-More` を返し、Gateway側で全件取得・filter・隠れtruncateをしません。

create/update/delete/restoreは `PUT /api/tasks/operations/{operation_id}` だけを使います。呼出側は `operation_id` を必須で保持してください。update/deleteには直前の `task_read` が返した `expected_updated_at`、restoreには `deletion_batch_id` が必須です。updateの `changes` では省略と明示的なnullを区別します。statusは同じURLへのGETです。operation結果の保存snapshotと現在のTaskは、サーバーが返す別項目のまま保持します。create/update結果のtimestampはCalendar反映前の値なので、次のmutationには流用せず再度 `task_read` します。

通信が不明な場合は同じoperation IDと同じpayloadでstatus照合し、必要な再送もその組合せだけにします。異なるoperation ID、旧CRUDへのfallback、provider自動切替は行いません。旧serverがoperation endpointを持たずcode-less 404を返す場合は `endpoint_unavailable` となり、Task not foundとは扱いません。

## DocsとKnowledge

Docs searchはID/title候補だけを返し、本文excerptを生成しません。read/edit-readは不透明cursorをそのまま渡します。edit readの最後の完全pageにだけ `write_token` を返し、未完pageには含めません。changeset再送では同じ `write_token`、`operation_id`、changesetを保持します。MCPはcursorの自動進行をしません。

Knowledgeは `/api/knowledge/plugin/*` の専用endpointだけを利用します。Source全件取得のadmin bypassは使わず、Projectの既存global-admin ACLを維持します。body fingerprintとpaging情報を保ち、`root_path`、`token`、`access_policy` は応答から除外します。

## API依存と確認

管理APIは41側の新operation/read/changeset/plugin契約に依存します。旧AoiTalk serverのfixtureではoperation PUTとGETのcode-less 404を両方 `endpoint_unavailable` とし、mutationを発行しません。202受理は副作用完了を意味しないため `effects=pending` をそのまま返します。Calendar通知などの副作用状態はoperation statusの `done`、`warning`、`unconfirmed` として確認します。

専用Tunnelを用意する前のoffline fixture確認はlive preflight完了を意味しません。2 runtimeのport/profile、runtime key権限、dispatcher arm状態、watchdog、dummy job保持はrootの運用preflightで確認してください。
