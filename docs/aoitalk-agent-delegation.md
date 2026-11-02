# AoiTalk の正準 Agent へローカル委譲する

> **現行構成（Phase B `cc84cc7` 以降）:** 汎用 Gateway は常に78 toolsを公開し、AoiTalk tools 35個はAoiTalk専用 MCP 接続だけで公開します。`HOME_MCP_AOITALK_ENABLED` は無効な旧flagで、catalogに影響しません。

この経路は ChatGPT のモデル差し替えではありません。Gateway は目的と権限を
AoiTalk の既存タスク API に渡し、AoiTalk が正準 Agent / AgentRevision / AgentRun /
work と既存の共有ローカル runtime を使って実行します。Gateway に別の LLM loop、
モデルカタログ、Strata 起動管理は追加しません。

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

readiness が false の環境では `command_runtime_unavailable` (HTTP 409) を返し、
モデルや任意ホスト shell、別の実行系へ fallback しません。このエラーは受付結果不明ではなく
設定/環境上の不備として扱い、盲目的に再送しません。

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
