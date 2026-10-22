# AoiTalk の Agent へローカル委譲する

この経路は ChatGPT のモデル差し替えではありません。Gateway は目的と権限を
AoiTalk の既存タスク API に渡し、AoiTalk が自身の Agent / AgentRevision / AgentRun /
work と共有ローカル runtime を使って実行します。Gateway に別の LLM loop、
モデルカタログ、ローカルモデル runtime の起動管理は追加しません。

## 構成と依存関係

```text
ChatGPT → Gateway（MCP / Tunnel）→ AoiTalk API → Agent / AgentRun / work → 実行 runtime
AoiTalk → Gateway（ローカル stdio、read-only の 4 tool）
```

- AoiTalk 連携は任意機能です。AoiTalk 本体が同じ PC にインストール・設定済みで、
  Gateway から認証済みの API に到達できる必要があります。Gateway は AoiTalk を
  インストールも設定もしません。
- 専用の `aoitalk_*` ツール（`aoitalk_local_task_*`、`aoitalk_agent_task_*`、
  `aoitalk_cli_session_*` など）は、provider（API 型、ローカル Strata / Qwen 型、
  各 CLI 型）を問わず、すべて AoiTalk 側の API とポリシーを必要とします。
  AoiTalk が停止・未設定の場合は接続エラーを返し、別の provider や
  別の実行系へ fallback しません。
- Gateway 自体の汎用コマンド実行ツール（Codex などの CLI を直接起動できる経路）は
  AoiTalk に依存しません。AoiTalk が無くても動作し、AoiTalk の Agent 権限や
  task 契約は適用されません。両者を混同しないでください。
- AoiTalk を無効（`HOME_MCP_AOITALK_ENABLED` 未設定または `0`）にしても、
  通常の Gateway ツールは引き続き使えます。

## ChatGPT → Gateway → AoiTalk

Gateway の `.env` で `HOME_MCP_AOITALK_ENABLED=1` を opt-in し、既存の認証設定を使います。
秘密値は MCP 引数・モデル context に渡しません。

`aoitalk_local_task_start` は任意の `agent_id`, `provider`, `model`, `effort` を受け付けます。
`agent_id` を省略した場合は、AoiTalk の設定で既定 Agent が明示されていればそれを使い、
設定がなければ `agent_not_configured` を返します。適当な Agent は選びません。
`provider` / `model` / `effort` は、Agent の公開済み revision の route に対する期待値であり、
別 route を強制する値ではありません。不一致は推論前に `agent_route_mismatch` で拒否されます。

初期疎通の例:

```json
{
  "goal": "READMEを読み、起動手順を3点で要約する",
  "workspace": "<workspace-path>",
  "agent_id": "<agent-id>",
  "provider": "<provider>",
  "model": "<model-id>",
  "mode": "read_only",
  "allow_commands": false,
  "idempotency_key": "<unique-key>"
}
```

Agent の設定・revision は AoiTalk の Agent 管理から行います。AoiTalk 全体の既定モデルや
他の Chat / Agent の route を変更しないでください。route・共有 runtime の readiness・
権限を確認してから開始します。`effort` は、その Agent が有効化している値に
一致させる場合だけ渡します。

### 受付と結果の読み方

- start は受付と永続 `task_id` を返します。受付成功は作業完了ではありません。
- status / list / 接続診断はモデルをロードしません。結果には公開 identity、
  `requested_route` / `resolved_route`、`execution_state` が含まれます。
- `task_id` と `agent_run_id` は別の ID です。
- 待機は `waiting`、承認待ちは `waiting_approval`、安全に再開できない中断は
  `interrupted` で区別します。実行側が通知していない進捗は推測しません。
- 業務エラー（`ok=false` と固定 error code）と、MCP の引数・transport 失敗（`isError=true`）は
  別のものとして扱います。通信成功や HTTP 200 だけを操作成功とはみなしません。
- 長い最終回答は `aoitalk_agent_task_result` で取得します（詳細は
  [Generic AoiTalk Agent task bridge](aoitalk-agent-task-bridge.md)）。

通信切断などで受付結果が不明な場合は、同じ内容・同じ idempotency key で再取得してください。
別の Agent / route / workspace / 権限への変更は別依頼・別 key とします。
冪等性は task 作成の重複防止であり、外部書込やコマンド副作用の exactly-once 保証ではありません。
不明な副作用を盲目的に再送しないでください。

## capability と readiness

通常の Agent は `read_only` / `allow_commands=false` で接続確認します。
`read_only` のまま `allow_commands=true` を指定した要求は拒否されます。

コマンドを必要とする依頼は `mode=mutate` と `allow_commands=true` を明示し、次を満たす必要があります。

- Agent が `command_execute` capability を持つこと。
- 設定済みの harness root と、AoiTalk の隔離実行 scope の範囲内であること。
- モデル起動前に、コマンド runtime の readiness（隔離実行環境の利用可否）が確認できること。

readiness が false の環境では `command_runtime_unavailable`（HTTP 409）を返し、
モデルや任意のホスト shell、別の実行系へ fallback しません。これは受付結果不明ではなく
設定・環境上の不備なので、盲目的に再送しません。接続の復旧だけでコマンドの許可条件を
満たしたとは扱いません。

Git 書込は既定で拒否されます。`allow_git_commit=true` は明示 path の `git add` と
制限付き `git commit`、`allow_git_push=true` は通常のブランチ refspec による `git push` だけを
依頼 scope 内で許可します。どちらも `mode=mutate`、`allow_commands=true`、Agent の既存
`command_execute` authority が必要です。force / delete push、別 workspace / repository の指定、
reset / clean、Git remote / config の変更は拒否します。これらの flag は Agent の権限や
実行 runtime / network capability を増やしません。`allow_adb=true` と `expected_branch` は
Agent 経路では未対応です。

### 検証コマンド要否 `requires_command_verification`

`aoitalk_agent_task_start`（および `agent_id` 指定の経路を通る `aoitalk_local_task_start`）は、
任意の `requires_command_verification`（厳密な boolean）を受け付けます。

- 省略・`null`: 何も送りません。AoiTalk が goal / completion_criteria の文面から実行意図を推定します。
- `true` / `false`: そのまま送信し、文面による推定より優先されます。
- `true` は「最後のファイル編集より後に成功したコマンド実行が必要」の意味です。
  `allow_commands=false` との併用は矛盾で、AoiTalk が 422 で拒否します（Gateway は権限を変更しません）。
- `false` は、静的なレビューやファイル内容の確認など、コマンド実行を要しない作業の宣言です。
  テストを実行した、という主張ではありません。
- boolean 以外（`0` / `"true"` など）は認証・HTTP の前に `invalid_input` で拒否します。
- 旧 local-task 経路（`agent_id` 省略、または旧 `provider` 指定）では強制できないため、
  `null` 以外は黙って無視せず `invalid_input` で拒否します。

## AoiTalk → ローカル Gateway stdio

AoiTalk の MCP クライアントが、別 venv の Gateway の Python を subprocess として stdio で
起動します。Python モジュールを AoiTalk へ直接 import せず、Tunnel も経由しません。
起動設定は AoiTalk の正規 MCP 設定・管理境界で行い、実行 scope は AoiTalk の信頼された
adapter が渡します。モデルが workspace / owner / AgentRun scope を指定・拡大する設定にはしません。

- command: `<repo>/.venv/Scripts/python.exe`（Windows の例）
- args: `<repo>/scripts/gateway_stdio.py` の絶対パス
- cwd: `<repo>` の絶対パス
- 子プロセス専用 environment:
  - `HOME_MCP_PROFILE=local_agent_read_only`
  - `HOME_MCP_WORKSPACE=<認可された絶対workspace>`
  - `HOME_MCP_AGENT_RUN_ID=<AgentRun ID>`
  - `HOME_MCP_OWNER_ID=<認証から得た owner ID>`
  - `HOME_MCP_AOITALK_ENABLED=0`

scope の検証と Agent の有効性・権限の確認は、AoiTalk が各 tools/call の直前に行います。
1 child は 1 run のみに属し、取消・権限剥奪・終了時は AoiTalk の既存 lifecycle でその child だけを閉じます。
環境変数の ID は照合・監査情報であり、認証資格情報ではありません。
子環境へ渡すのは OS / Python の必須値と scope の allowlist のみで、Gateway の launcher も
同じ allowlist を再適用し、不要な credential を保持しません。通常 Gateway の環境は変更しません。

この profile が公開するツールは次の固定 4 個だけです。

| Tool | 制限 |
|---|---|
| list_directory | path、max_results 1–500 (default 100) |
| path_info | path の通常ファイル/ディレクトリ metadata |
| read_text | path、UTF-8、max_chars 1–24000 (default 12000) |
| search_text | root、query 1–1000文字、filename glob、max_results 1–100、max_files 1–500、timeout >0–10秒 |

scope 外、`..`、Windows device / UNC / ADS、曖昧な末尾、symlink / junction / hardlink、
既知の credential path を拒否します。ファイルを開いた後にも descriptor / handle の
最終パスを検証してから内容を読みます。大きなファイルと探索件数は制限し、切り詰めを明示します。
この profile は汎用 host shell の OS sandbox ではありません。任意コマンド、書込、環境変数、
job / browser / desktop、画像、外部 HTTP、AoiTalk 再委譲、clip ingest は公開も実行もしません。
直接 tools/call や汎用 call_mcp_tool 名を使っても禁止ツールは実行できません。
未知の profile は起動失敗とし、通常の無制限 catalog へ fallback しません。

## 検証

実 GPU・外部 LLM・実 desktop を使わない fixture テストを先に実行します。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_aoitalk_tasks.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_local_agent_profile.py -v
```

次に、AoiTalk 側の MCP クライアントから Gateway の stdio child を起動し、
initialize → tools/list → 無害な read_text → 禁止 tools/call の拒否 → 切断を確認します。
AoiTalk と Gateway は別の venv・別の MCP SDK バージョンで動かすことがあるため、
単一 venv のテストを cross-version 検証と呼ばないでください。
通常 Gateway の既存テストも実行し、連携 OFF や AoiTalk 停止でも通常ツールが使えることを確認します。

## 反映と OFF への戻し方

コードと非 GPU テストの完了は、稼働設定の変更・再起動・MCP metadata 更新・実モデルでの受入とは別です。
他の作業が動いている間は、稼働中の AoiTalk / Tunnel / ローカルモデル runtime を切り替えないでください。
安全なタイミングで起動設定を反映し、Gateway を再接続して、ChatGPT の
[公式の MCP metadata refresh 手順](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)
で追加 schema を再取得し、新しい会話で対象 tool を再評価します。
稼働確認では requested / resolved route と実 worker identity を照合します。

OFF へ戻す場合は、AoiTalk 側の対象 scoped 接続を解除し、その所有 child を閉じます。
ChatGPT 側の委譲機能も止める場合にだけ、Gateway の `HOME_MCP_AOITALK_ENABLED=0` を設定し、
安全なタイミングで再接続と metadata 更新を行います。Agent 全体の route や共有モデルは変更しません。
