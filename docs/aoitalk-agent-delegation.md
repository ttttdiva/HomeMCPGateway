# AoiTalk の正準 Agent へローカル委譲する

> **現行構成（Phase B `cc84cc7` 以降）:** 汎用 Gateway は常に78 toolsを公開し、AoiTalk tools 35個はAoiTalk専用 MCP 接続だけで公開します。`HOME_MCP_AOITALK_ENABLED` は無効な旧flagで、catalogに影響しません。以下の日付付き受入記録とtool数は記録当時の履歴です。

この経路は ChatGPT のモデル差し替えではありません。Gateway は目的と権限を
AoiTalk の既存タスク API に渡し、AoiTalk が正準 Agent / AgentRevision / AgentRun /
work と既存の共有ローカル runtime を使って実行します。Gateway に別の LLM loop、
モデルカタログ、Strata 起動管理は追加しません。

## 現在の適用状態（2026-10-13）

41 の統合済み HEAD `b9db6a3ec359761d377f8dc902acbc60591e887e` で、
Gateway のタスク関数 → 認証済み AoiTalk API → 正準 Agent / AgentRun / Work →
実 Strata / Qwen の読み取り専用実行を確認しました。別の実行基盤は追加していません。

既存 Agent 基盤で Gateway Strata を作成し、専用のモデル設定 profile
`gateway-strata-local` を割り当てました。権限は `workspace_read`、
通常の `general_worker` を使用し、他の Agent の route は変更していません。
この初回受入は Agent ID を明示しています。下記のセッションC受入で省略時の既定 Agent も確認しました。

初期試験で権限不足、未対応 effort、Strata artifact の旧ドライブ参照を検出しました。
既存の権限設定と明示的な thinking effort を適用し、実タスクの runtime snapshot に
一致するモデル配置へ既存 relocation helper で設定を更新した後、再試験が成功しました。
モデルファイルの移動・削除や検証ガードの緩和は行っていません。

成功時は LLM 3 回、読取ツール 2 回、書込 0 件、コマンド 0 件です。
プロンプトに含めなかった marker を実ファイルから取得し、
AgentRun / Work の succeeded とタスクの completed を確認しました。
2026-10-13 04:35 UTC の読み取り専用 coordinator status では runtimes は空で、
試験後のモデル runtime 解放も確認しています。

この実測は Gateway の実装関数からの受入です。ChatGPT 側の接続メタデータ更新後に
新しい 5 ツールを実際に呼べること、実モデルによる書込・コマンド・ADB・Git 操作まで
確認したものではありません。安全な読取成功と、すべての開発操作の完成を区別します。
証跡は [読み取り専用受入](evidence/gateway-strata-readonly-20261013.json) を参照してください。

### セッションCのschema・接続受入

80の5 task toolsは、入力のenum・範囲・配列上限・nullable・requiredと、
出力のtask/route/service/result/eventsのnested schema・array・nullable・`$defs`/`$ref`を公開します。
出力検証は既存の `CallToolResult` の `structuredContent` に行い、元のJSONに
任意フィールドの既定値を追加したり、将来の業務フィールドを削除したりしません。
業務エラーは `ok=false` と固定error codeを保持し、MCPの引数/transportエラーの
`isError=true` と区別します。tool名・受付後のLLM loop・権限の境界は既存契約を使います。

2026-10-13の実別venv受入では、41 `venv` のMCP 1.30.0から80 `.venv` のMCP 2.2.0を起動し、
通常84 toolsと5 task toolsの全schema、既成功taskの構造化status再取得、連携OFFの76 tools、
接続不可時の業務エラーと通常file toolの成功、scoped childの4 tools・scope拒否・禁止tool拒否を確認しました。
各stdio接続を閉じた後に、この試験が起動した所有child PIDが消えたことも確認しています。
初回の接続不可受入には閉じたloopback portを使っています。さらに23:56 JST、A/Bの正式停止枠で
実41のAPI 3000 / Next 3002が閉じた状態でも、通常stdio84 toolsの
`list_directory` が成功しました。task listは `ok=false / connection_error / isError=false` を返し、
試験所有child 65240 / 68912は終了しました。実測7.816秒で、モデル・task受付・wakeは発生していません。
[実41停止時のGateway回帰](evidence/gateway-stopped-41-20261013.json) を参照してください。
2026-10-14のAPP_COLD追加枠では、正式停止通知後の開始port検証が不成立で、stdio/tool呼出前に中止しました。
後続の読み取り専用観測では3000 / 3002に新しいPIDがあり、A/Bへ即時共有しています。
この追加枠をPASSにはせず、[停止前提の失敗receipt](evidence/gateway-app-cold-stop-failure-20261014.json) に分けて保持します。
A/Bは80と41のコードが同一であるため、Gatewayの停止時独立性には既存BOTHの実受入を再利用し、
Cの追加停止チェックを不要としました。APP_COLDのstrict before・起動受入はBが別途所有します。
fixtureはtask 30、profile 14、telemetry 5、通常stdio 5の計54件がPASSしました。
実stdio再接続後の同じIdempotency-Keyは1 taskを再取得し、nested結果・nullable・空配列・
業務エラーを保持し、schema違反のmodeはAPIへ送信前に拒否します。
大量一覧の出力予算境界では数値やID/enumを文字列markerへ変換せず、収まらない値を省略し、
task IDと状態を両方保持できないtaskは一覧へ出しません。`output_truncated` で省略を明示します。
短いusernameとpublic protocolの名前/検証済みidentityが重なってもそれらを壊さず、
passwordを含む識別子・route・keyは省略し、通常payloadと未知keyの秘匿を維持します。
300通りの予算境界、実stdioの100-task大型一覧、credential衝突を回帰で確認しました。
[実別venvとschema receipt](evidence/gateway-task-catalog-20261013.json) を参照してください。
元依頼の10項目との対応・再利用した41側の隔離fixture/command証跡・残条件は
[セッションC受入対応](evidence/gateway-session-c-20261013.json) に記録します。

ChatGPT側は更新前の実画面で「Home MCP Gateway」が76 toolsを掲載し、
5 task toolsがないことを確認しました。80のschema commit `9f906d5`をpush後、
通常Tunnelを正規の `connect_tunnel.ps1` で再接続し、対象接続の「ツールを更新」で
84 tools（Write 78 / Read 6）と5 task名の掲載を確認しました。
新しいChatGPT会話からの `aoitalk_local_task_list(limit=1)` はGatewayのtelemetryでも
1回の成功呼出（873.783 ms、`ok=true`）と照合でき、評価中のstart/cancelは0件です。
ただし実画面はschema JSONを表示しないため、ChatGPTのモデルによるschema説明を
raw `inputSchema/outputSchema` 一致の証明にはしません。初回はこのCodexチャットにも
Gateway tool定義が未添付で、本人から一旦切断中との確認がありました。
[ChatGPT受入receipt](evidence/gateway-chatgpt-catalog-20261013.json) を参照してください。

2026-10-14、本人が受信側で5 toolsの展開済み公開型定義を保存catalogと照合し、
required・enum・nullable・array・nested・各範囲の整合を確認して、この範囲で受入成功としました。
実 `list(limit=60)` の認証成功、設定済み標準Agent、既存task `b4b555624e115ca99b06000128eb8785` の
completed/succeeded・LLM3/file2/command0も本人報告で確認しています。
生のinput/outputSchema JSONは取得できず、`$defs/$ref` の記述形式や非公開下位フィールドまで
完全一致したとはしません。この取得制約は公開型の受入と区別して記録します。
報告時のHEAD `794946e` と記録時の `6806222` の差分は資料だけで、Gateway実装は同一です。
[受信公開型の本人受入](evidence/gateway-chatgpt-received-types-20261014.json) を参照してください。
41の標準Agent設定保存・省略時解決はセッションAが所有し、80で別writerを作りません。
### Agent省略の通常経路での成功

2026-10-13 23:18 JST、稼働API source `7673b71` でAgent/provider/model/effortをすべて省略し、
通常stdio → 認証済みAPI → 正準AgentWork/Run → 実Strata → file tool → 最終回答まで成功しました。
task `b4b555624e115ca99b06000128eb8785`、Run `7c717f2a-5168-4c80-992d-0eba0795a32c`、
Work `f8e84447-80ef-40ae-9ecc-50de2ab4fa27` が対応します。
Agent/revisionは既存の `e9fd7414-492f-4a6a-aced-c5bd8f21f422` / `419bc59c-47f0-492f-8df7-f3bbfdd3ea85`、
requested route `{}`、resolved `openai_compatible_local / qwen3.8-flash-next-iq3_xxs / thinking` でした。
LLM 3回・file tool 2回・書込0・command0、実行768.988秒です。プロンプトに含めなかった
`session-c-e030287a62a64868` を `acceptance.txt` から読み、最終回答で正確に返しました。
ファイルmtimeはtask開始前を維持し、変更ファイルはありません。

実行中の新stdio再接続と終了後の同キーPOSTは同じtaskを再取得しました。
観測scriptはPID終了との競合で一度終了しましたが、再接続したstatusでcompleted/succeededと
同じidentity・最終回答を取得できました。新しいtaskや副作用の再投入はしていません。
所有Python/conhost/Strata PID `60056 / 61016 / 60488 / 64728` の全incarnation終了と
coordinatorの `runtimes=[]` を確認し、coordinator PID 2940は同じbrokerのまま独立生存しています。

モデル/pack/MTP/PLEの実argvはすべてF側でした。正常acquireが旧E snapshotから
実効F snapshot `4db9c61d7b089b5cedeb587e3cd69e1fb752e93ef74c2a5186269a6799b056a1` をpublishしました。
source `fb58e0d`、context 32768、prefill 256、vram reserve 2048、expert cache auto、port 8083、vision noneは維持しています。
この起動はBのAPP_COLD条件PASSとは分けます。受入証跡は
[Agent省略の実受入](evidence/gateway-default-agent-20261013.json) を参照してください。

セッションAの読み取り専用DB照合とWork API HTTP 200では、同じtask/source、Agent/revision、
sessionとRun/Workのsucceeded、Work lease owner/tokenの解放を確認しました。
Workは14:18:15.734652 UTC、Runはその0.124552秒後の14:18:15.859204 UTCに終端しています。
Run.resultとWork.result_summaryに `final_message` / report / checks / verified_tool_resultsが保存されています。
このheadlessタスクは依頼専用sessionを持ち、既存Chatへassistant行を自動追加しない契約です。
Chatへの明示取り込みは別のowner専用local-import操作であり、この受入では実施していません。
23:48 JSTの通常Gateway statusも同じcompleted結果を再取得できました。
[正準Run/Workの終端照合](evidence/gateway-default-agent-terminal-20261013.json) を参照してください。

### 復旧前の失敗を保持

2026-10-13 21:16 JST、セッションAが新しい稼働source `f948b72` で正規PATCH保存・
GET/DB再取得を確認した後、Agent/provider/model/effortをすべて省略して通常stdioから
read-only task `e901b00e5170575a9c3025cc8ebaceab` を作成しました。
新しいstdio接続で同じIdempotency-Keyを送ると同じtaskを再取得し、
Agent `e9fd7414-492f-4a6a-aced-c5bd8f21f422` / revision `419bc59c-47f0-492f-8df7-f3bbfdd3ea85`、
requested route `{}`、resolved `openai_compatible_local / qwen3.8-flash-next-iq3_xxs / thinking` が一致しました。
Run `de90447b-d76f-447e-89a4-591b30a5b864` / Work `d94c2796-842e-48c2-85cd-97b176bcb2a5`
まで正準経路へadmitされましたが、共有coordinator接続timeoutで3秒ほどでfailedになりました。
LLMカウンタ1は推論試行で、読取0・書込0・command0です。最終回答成功の証拠にはしません。

coordinator不在時のWindows接続が `TimeoutError` となり、既存のclientはtimeoutに対して
自動起動しないfail-closed契約を維持しました。既存の正規 `launch_coordinator` を41 venvから
実行した結果も、継承kill-on-close Job `0x00002000` の所有ガードで拒否され、launcher PID 24052は終了しました。
モデルworkerは起動していません。OS/Job設定、ガード、route、権限は変更していません。
この `0x2000` はCodexのcommand contextから起動したchildでの観測で、
稼働AoiTalk API/Supervisor自身のJob flagsを実証したものではありません。
実taskのtimeoutと、この明示launcherの所有ガード拒否を分けて扱います。
新しい稼働contextで正規の独立coordinatorをbootstrapできる境界をA/Bへ渡しました。
Aによる正規復旧後の再受入は上記のAgent省略タスクで成功しています。
[Agent省略の失敗receipt](evidence/gateway-default-agent-failure-20261013.json) に成功した解決/再取得と未達の実行を分けて記録します。

## ChatGPT → AoiTalk専用 MCP → AoiTalk

AoiTalk へのChatGPT連携ではAoiTalk専用 MCP 接続を有効にし、既存の認証済み AoiTalk APIを使います。
AoiTalk用の5 local task toolsは専用接続から呼び出します。汎用 Gatewayは常に78 toolsで、AoiTalk toolsを公開しません。
秘密値は MCP 引数・モデル context に渡しません。

`aoitalk_local_task_start` に任意の `agent_id`, `provider`, `model`, `effort` を追加しました。
省略した Agent は AoiTalk の DB 設定 `local_agent_tasks.default_agent_id` で明示されたものを
使います。設定がなければ `agent_not_configured` です。適当な Agent は選びません。
provider / model / effort は AgentRevision の正準 route の期待値であり、別 route を
強制する値ではありません。不一致は推論前に `agent_route_mismatch` となります。

初期疎通の例:

```json
{
  "goal": "READMEを読み、起動手順を3点で要約する",
  "workspace": "D:\\Dev\\example-workspace",
  "agent_id": "<既存Agent管理で選択したAgentのID>",
  "provider": "openai_compatible_local",
  "model": "qwen3.8-flash-next-iq3_xxs",
  "mode": "read_only",
  "allow_commands": false,
  "idempotency_key": "readme-check-001"
}
```

共通 Agent の設定・revision は既存の Agent 管理から設定します。AoiTalk 全体の既定モデルや
他の Chat / Agent のモデルを変更しないでください。モデル route、共有 runtime の readiness、
権限を確認してから開始します。`effort` はその Agent の正準値に一致させる場合だけ渡します。

start は受付と永続 `task_id` を返します。受付成功は作業完了ではありません。
status / list / 接続診断はモデルをロードしません。結果には公開 identity と
`requested_route` / `resolved_route`、`execution_state` を含みます。
`task_id` と `agent_run_id` は別の ID です。待機は `waiting`、承認待ちは
`waiting_approval`、安全に再開できない中断は `interrupted` で区別します。
`execution_state` は既存 work の状態をそのまま伝えます。モデル起動進捗は実行側が通知した情報だけを表示し、未観測の状態を推測しません。

通信切断などで受付結果が不明な場合は、同じ内容・同じ Idempotency-Key で再取得してください。
別の Agent / route / workspace / 権限への変更には別依頼としての判断が必要です。
タスク作成の冪等性は、外部書込やコマンド副作用の exactly-once 保証ではありません。
不明な副作用を盲目的に再送しないでください。

## 明示的なコマンド実行と readiness

通常の正準 Agent は `read_only` / `allow_commands=false` で接続確認します。
`read_only` のまま `allow_commands=true` を指定した要求は拒否されます。
コマンドを必要とする依頼は `mode=mutate` と `allow_commands=true` を明示し、
Agent の `command_execute` capability、設定済み harness root、既存の
`HarnessExecutionScope` による隔離実行境界を満たす必要があります。
41 の既存 WSL / bwrap 経路が network NONE、copy-on-write、secret masking を適用し、
モデル起動前にコマンド runtime の readiness を確認します。80 の read-only stdio profile
に shell ツールを追加する仕組みではありません。

2026-10-12 の対象 Windows 環境では、既存の実効設定にある Ubuntu-22.04 と
`/usr/local/bin/bwrap` を使った readiness probe が true であることを確認しています。
この確認では永続設定を変更していません。同じ設定の開発 checkout で Windows 上の
正準 Agent ORM smoke test が 1/1 成功しました（11.55秒、モデル応答のみ scripted fixture）。
構造化 `create_file` で無害な printf script を作成し、実際の scope 付きコマンド実行、
stdout / publication audit、実ファイルの読取、owner に結び付いた AgentRun の終端状態を
確認しています。この隔離実行経路を含む共有基盤は2026-10-13にmain `b9db6a3`へ統合済みです。
この段落のコマンド受入は scripted fixture です。2026-10-13の実Strata読取受入と、後述の実モデルcommand受入とは別の証拠です。
同時点の共有 coordinator の profile / runtime 登録は空で、既存の無関係な llama GPU
プロセスを採用・切替して検証したものではありません。
readiness が false の環境では `command_runtime_unavailable` (HTTP 409) を返し、
モデルや任意ホスト shell、別の実行系へ fallback しません。このエラーは受付結果不明ではなく
設定/環境上の不備として扱い、盲目的に再送しません。

2026-10-14の本人報告では、Agent省略・`mode=mutate / allow_commands=true` の隔離pwd相当試験
（key `chatgpt-gateway-realmodel-pwd-20261014-a1`）は `connection_error`、`submission_uncertain=false`、
task IDなしで受付できませんでした。その後のlistも失敗し、通常Gatewayのfile操作は成功しています。
報告時刻がなく、特定のA/B再起動を原因とは断定しません。API復旧・現在のAgent権限・既存隔離readinessはAが確認します。
既存標準Agentの受入権限は `workspace_read` のみです。接続復旧だけでコマンドの許可条件を満たしたとは扱いません。
同じkeyの再送は同一依頼に限定し、Agent/route/scopeを変更する場合は別依頼・別keyとします。

2026-10-14、Aは専用QA API `127.0.0.1:5572` の通常HTTP → 正準local task →
AgentWork/Run → 実Strata/Qwen → 隔離commandの受入を完了しました。
source `f9f72ea`、task `229feb76a24c5a4f9d519ac343838eeb`、
Run `e9ae2c8b-20d2-47a8-8850-54d5fd58c984` / Work `9ce6b90c-39c9-42ac-8869-0b1cd3e4fbd1` はsucceededです。
QA専用Agent `781a1373-ff44-4465-bb3b-108dafbf080a` / revision `617be529-95ac-4919-bd7d-ab67b9d08e62` は
read/write/commandの3 capabilitiesを持ち、strict child workspace、既存trusted WSL/bwrap、network NONE、有限budgetを使用しました。
標準Agent `e9fd…` の読取権限は変更していません。

実モデルは7 LLM / 6 tools / 3 command要求で、2回の有限な失敗から回復し、
`pwd; ls -1a; echo sandbox-ok` をexit 0で完了しました。markerの作成と読戻し、marker以外の追加タスクファイル0を確認しています。
receiptの終了記録はunfinished background jobs 0、generation leases 0、モデルrelease後のruntime 0、
所有API5572/model8083の閉鎖です。共有brokerは保持しています。
CはAのreceipt SHA256を照合して、この限定結果を元依頼のcommand条件へ対応づけました。
このタスクはQAの通常HTTP経路で、元のChatGPT keyの再投入、production API3000の復旧受入、formal Employee/SID endpointの受入ではありません。
[実モデルの隔離command受入](evidence/gateway-strata-command-qa-20261014.json) を参照してください。

セッションCの受入は、本人が受け入れた5 toolsの公開型、標準Agent省略の実読取、実別venv/scoped lifecycle、
停止時の通常Gateway維持、上記の正規QA command経路を対応づけて完了です。生schema JSONの取得制約は記録に保持します。

正準 Agent tool は既定でGit書込を拒否します。`allow_git_commit=true` は明示pathの
`git add` と制限付き `git commit`、`allow_git_push=true` は通常のブランチrefspecによる
`git push` だけを依頼scope内で許可します。どちらも `mode=mutate`、
`allow_commands=true`、Agentの既存 `command_execute` authority が必要です。
force/delete push、別workspace/repository指定、reset/clean、Git remote/config変更は拒否します。
これらのtask flagはAgentRevision authorityや実行runtime/network capabilityを増やしません。
正準 Agent の `allow_adb=true` と `expected_branch` は引き続き未対応です。

### 検証コマンド要否 `requires_command_verification`

`aoitalk_agent_task_start`（および `agent_id` 指定の正準経路を通る `aoitalk_local_task_start`）は、
任意の `requires_command_verification`（厳密な boolean）を受け付けます。

- 省略・`null`: 何も送りません。AoiTalk が goal / completion_criteria の文面から実行意図を推定します（従来どおり）。
- `true` / `false`: そのまま送信し、文面による推定より優先されます。
- `true` は「最後のファイル編集より後に成功したコマンド実行が必要」の意味です。`allow_commands=false` との併用は矛盾で、AoiTalk が 422 で拒否します（Gateway は権限を変更しません）。
- `false` は、静的なレビューやファイル内容の確認など、コマンド実行を要しない作業の宣言です。テストを実行した、という主張ではありません。
- boolean 以外（`0` / `"true"` など）は認証・HTTP の前に `invalid_input` で拒否します。
- 旧 local-task 経路（`agent_id` 省略、または旧 `provider` 指定）では強制できないため、`null` 以外は黙って無視せず `invalid_input` で拒否します。

## AoiTalk → ローカル Gateway stdio

41 の既存 MCPPlugin / ClientSession を使い、別 venv の 80 の Python を subprocess として
起動します。Python モジュールを 41 へ直接 import しません。Tunnel 経由ではありません。
起動設定は既存の正規 MCP 設定/管理境界で行い、実行 scope は信頼された 41 の adapter が渡します。
モデルが workspace / owner / AgentRun scope を指定・拡大する設定にしません。

- command: `D:\Dev\80_HomeMCPGateway\.venv\Scripts\python.exe`
- args: `D:\Dev\80_HomeMCPGateway\scripts\gateway_stdio.py` の絶対パス
- cwd: `D:\Dev\80_HomeMCPGateway` の絶対パス
- 子プロセス専用 environment:
  - `HOME_MCP_PROFILE=local_agent_read_only`
  - `HOME_MCP_WORKSPACE=<認可された絶対workspace>`
  - `HOME_MCP_AGENT_RUN_ID=<正準AgentRun ID>`
  - `HOME_MCP_OWNER_ID=<認証から得たowner ID>`

認証済み scope の検証と現時点の Agent 有効性・権限の確認は、41 が各 tools/call の直前に行います。
1 child は 1 run のみに属します。取消・権限剥奪・終了時は 41 の既存 lifecycle を使って
その child だけを閉じます。環境変数の ID は照合/監査情報であり、認証資格情報ではありません。
41 は OS/Python 必須値と scope の allowlist のみを子環境に渡します。80 の launcher も
同じ allowlist を再適用し、不要な credential を保持しません。子環境には `home_mcp_gateway/local_agent_profile.py` が `HOME_MCP_AOITALK_ENABLED=0` を常に強制設定します。これは旧 AoiTalk tools の登録を意味せず、汎用 catalog も変更しません。通常 Gateway の環境は変更しません。

ローカル profile の公開ツールは固定の 4 個だけです:

| Tool | 制限 |
|---|---|
| list_directory | path、max_results 1–500 (default 100) |
| path_info | path の通常ファイル/ディレクトリ metadata |
| read_text | path、UTF-8、max_chars 1–24000 (default 12000) |
| search_text | root、query 1–1000文字、filename glob、max_results 1–100、max_files 1–500、timeout >0–10秒 |

既定で read-only / allow_commands=false の接続確認を行います。`allow_commands` 未指定は
Gateway からフィールドを送らず、AoiTalk が正準 Agent 経路では false を適用します。
明示的な legacy 実行経路の従来のサーバー側既定値は維持します。明示 true を
権限がないまま false に丸めて受け付けることはしません。

scope 外、`..`、Windows device/UNC/ADS、曖昧な末尾、symlink / junction / hardlink、
既知 credential path を拒否します。ファイルを開いた後にも descriptor/Windows handle の
最終パスを検証してから内容を読みます。大きなファイルと探索件数は制限し、切り詰めを明示します。
この profile は汎用 host shell の OS sandbox ではありません。任意コマンド、書込、環境変数、
job/browser/desktop、画像、外部 HTTP、Jev、AoiTalk 再委譲、clip ingest は公開も実行もしません。
直接 tools/call や汎用 call_mcp_tool 名を使っても禁止ツールを実行できません。
未知 profile は起動失敗し、通常の無制限 catalog へ fallback しません。

SDK の業務結果 `ok=false` と `isError` / transport 失敗は分けて扱います。
成功した通信や HTTP 200 だけを操作成功とはみなしません。

## 検証と反映

まず実 GPU / 外部 LLM / 実 desktop を使わない fixture テストを実行します。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_aoitalk_tasks.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_local_agent_profile.py -v
```

さらに 41 の venv の実 MCP SDK から、80 の venv の実 MCP SDK を起動して
initialize → tools/list → 無害な read_text → 禁止 tools/call → 切断を検証します。
41 は mcp 1.x、80 は 2.x を維持します。単一 venv のテストを cross-version 検証と呼びません。
通常 Gateway の既存テストも実行し、Phase B で汎用 Gateway から AoiTalk tools を撤去した後の 78 tools が、旧flagの値・AoiTalk 接続の有無・AoiTalk 停止のいずれでも同一であることを確認します。

コードと非 GPU テストの完了は、稼働設定の変更・MCP metadata 更新・実 Qwen 受入とは別です。
他の作業が動いている間は稼働 AoiTalk / Tunnel / Strata / llama-server を切り替えません。
AoiTalk toolsをONにする場合はChatGPTのAoiTalk専用 MCP 接続を有効にし、対象接続の「ツールを更新」(Refresh)を実行します。
汎用 Gateway に残る旧 `aoitalk_*` 名が `Unknown tool` になる場合も、その接続でRefreshします。この catalog 移行では、接続済み catalog が古いこと自体は Refresh で足り、runtime プロセスの再起動は要りません。変更した runtime のコードや設定を稼働へ反映するために再起動が必要な場合は、その通常の runtime 再起動を別途行います。Refreshで解消しない場合は、接続を削除して再追加します。
専用接続のON/OFFを切り替えても汎用 Gateway の78 toolsは変わりません。稼働確認では requested/resolved route と実 worker identity を照合します。

OFFへ戻す場合はChatGPTでAoiTalk専用 MCP 接続を無効にします。41の対象scoped接続を止める場合は、その所有childを閉じます。Agent全体のrouteや共有モデルは変更しません。
