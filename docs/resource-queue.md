# ローカル Resource Queue（minimum pump）

この文書は、共有ローカルリソースを一つの controller から順序づけて使うための、最小の SQLite queue の運用境界と設計を記録する。queue はリソースごとの排他、固定された operation、再起動後に照合できる dispatch 証跡を持つ。queue が扱うのはローカルの intent と dispatch の記録であり、推論・端末サービス・ハードウェア操作そのものを実装するものではない。

## 運用上の約束

同じ `resource_id`（運用上の `resource_key`）を使う新しい作業は、必ずこの queue の controller を通して dispatch する。実装が受け付ける kind は `phone` と `gpu` である。phone と GPU は別のリソースであり、phone の lease が GPU の実行を止めたり、GPU の稼働数を phone の枠として数えたりしない。共有資源を一つの controller に束ねるという意味での「single resource controller」であって、マシン上の全プロセスを強制的に捕捉する仕組みではない。

すでに queue の外で動いている job は、自動的に queue の所有物にならず、resource が空いているとも自動判定されない。最初の ownership handover は root/operator が明示的に行い、外部 job の識別子、現在の状態、観測時刻、必要な actor attestation を reviewed input として登録する。その handover 以降に開始する作業を queue 経由にする、という運用境界を作る。手動起動や別プロセスを後から一覧検索して「無かったこと」にする機能はない。

初期 attestation には空でない `device_identity` を必ず含める。`attestation_hash` は登録レコードの digest であり、物理 device identity の代替にはしない。phone は `dedicated_thread_id` も必須で、空でない dedicated thread ID は resource 間で一意でなければならない。device identity の変更は、queued/running の nonterminal operation がある間は user conflict として拒否される。

dedicated thread と operation Gateway job は同じものではない。thread は resource controller の長寿命な調停単位、operation は一つの immutable input/procedure/payload に対する一回の作業単位である。operation が Gateway job を使う場合も、thread の lease と Gateway job の process identity / result は別々に記録する。登録時に固定された device identity と resource attestation hash は operation に snapshot され、release receipt にも同じ identity を要求する。既存の `job_start` 固定キー経路を使う job は `.runtime/jobs` に durable metadata とログを持ち、thread の状態だけから実行成功を推測しない。

### 同一 thread の actor 運用契約

actor の startup envelope は、現在の `current_operation_id`、`current_actor_job_id`、`current_thread_id` をそれぞれ明示し、過去の terminal job は `prior_terminal_job` の別フィールドで示す。自分の job を別 actor の job と取り違えず、同じ job への `job_wait` を自分自身の待機として発行しない。actor は割り当てられた operation を実行し、安全に resource を解放できる bound release receipt、raw artifact reference、最小限の result を出したら、速やかに turn を終了する。重い原因分析は case reviewer の責務であり、controller は依存関係のない先行 case の review 完了を、次の ready operation の dispatch 条件にしない。idle token polling は行わない。

## queue の保存と pump

queue の SQLite は `.local/resource-queue/state.sqlite3` に置き、同じディレクトリの `resource_queue.lock` を OS singleton に使う。Gateway job の実体と stdout/stderr は既存の `.runtime/jobs/` を使う。SQLite は WAL、`synchronous=FULL` とし、intent・claim・lease の競合を `BEGIN IMMEDIATE` で直列化する。pump はプロセス間 OS singleton を取得してからこの transaction を行うため、二つの pump が同じ resource を同時に claim しない。singleton は物理 device を止める仕組みではなく、queue controller の同時実行を一つにする仕組みである。

これは常駐 daemon や event receiver ではない。idle 中の token polling は行わず、root または明示的な手動 caller が次の境界で `Controller.pump()` を呼ぶ。queue の core API は `Queue(directory)`、`register_resource(...)`、`enqueue(...)`、`review(...)`、`cancel(...)`、`heartbeat(...)`、`status(...)`、`receipts(...)` と `Controller(queue, runner, lease_seconds, owner).pump()` / `.reconcile()` である。実装が確定した CLI がある場合も、これらの API の transaction 境界を迂回して runner を直接呼んではならない。

* enqueue の commit 後
* dependency の predecessor が完了したと確認した後
* Gateway job の completion/status を照合した後
* unknown dispatch の reconcile が完了した後

pump はその呼出しで処理できる件数だけを進めて戻る。OS 再起動時に自動起動する新しい登録済み daemon、webhook、外部 event subscription はこの minimum scope に含まれない。

lease の期限は reclaim 許可ではない。期限を過ぎても lease row は残り、次の controller が旧 owner/generation を reconcile してからでなければ同じ resource を別 operation に割り当てない。

claim は ready の `priority DESC, enqueue_seq ASC` で選び、blocked の先頭を飛ばす。priority 更新は queued（blocked/ready）のみで、running を奪わず次の claim に反映する。FAILED でも release receipt が揃うまでは資源を保持し、receipt 保存・terminal 化・lease 解除を同じ transaction で行った後に次の ready を選ぶ。

## immutable operation と fixed key

root/operator は dispatch 前に、review 済みの入力、procedure、payload、依存関係、`resource_id`、resource attestation hash、device identity を canonical な snapshot として確定する。snapshot の bytes/digest と作成者の review/authorization ref を保存し、dispatch 後に本文、手順、cwd、実行ファイル、環境 override、依存関係、device binding を編集できない。status に本文を表示する必要はなく、ログには digest と安全な識別子だけを出す。

各 operation には変更されない `op_key` を一つだけ割り当てる。現在の codec は operation ID の UTF-8 bytes の SHA-256 を `rq1:<hex>` として key にし、その exact key を既存 `stable_job_id` に渡して Gateway job ID を導く。同じ operation の再開、結果照合、未知応答後の再試行は同じ key と同じ payload を使う。新しい payload、別の job、別の resource へ勝手に re-key して成功扱いにしない。key の予約は intent が外部 I/O より先に durable になったことを示すが、分散システムの「exactly once 実行」や相手側の idempotency を保証するものではない。

`Queue.enqueue` は `operation_id`、`resource_id`、`project`、`input`、`procedure`、`launch`（`request`/`payload` は同じ値の alias）、依存関係、priority、任意の `review_ref` を受け取る。input/procedure/payload と resource binding は canonical JSON/hash として保存する。同じ operation ID を同じ内容で再 enqueue するのは idempotent だが、いずれかを変えると `OperationConflict` になり元の row は変わらない。enqueue は `review_ref` の有無にかかわらず常に未 review (`blocked`, `review_required`) で作成される。`Queue.review(operation_id, input_hash, procedure_hash, ...)` が保存済み input/procedure hash と review ref を一致確認してから ready にする。review は payload や実行結果を生成せず、モデル推論や推論結果による unlock も行わない。

## intent、claim、lease、dispatch

一つの `Controller.pump()` pass は概ね次の順で進む。Runner の最小契約は `start`、`lookup`/`status`/`inspect` のいずれかによる照合、`request_cancel`、および `verify_release`（または `inspect`）である。controller 以外のコードが runner を呼ぶ経路は用意しない。

1. `BEGIN IMMEDIATE` の中で、snapshot digest、op key、依存関係、未処理状態を再検証し、dispatch intent と append-only attempt 番号を commit する。
2. 同じ transaction で `resource_id` の claim と lease owner、generation fence、期限を確定する。期限切れの古い claim は、owner/generation が一致しない限り後続の書き込みを通さない。
3. transaction の外で、固定された builder が実行 request を作り、verifier が key、payload digest、実行ファイル、cwd、引数を再照合してから runner を呼ぶ。
4. runner の結果と capture を新しい receipt として保存する。receipt の append に失敗した、または応答が失われた場合も、実行結果を成功と推測せず lease を保持した `unknown` とする。

`unknown` は terminal failure でも自動成功でもない。operator/root は既存の Gateway job metadata、process identity、stdout/stderr、terminal result を同じ op key と突き合わせて reconcile する。外部側に受理されたか分からないまま lease を解放したり、任意の回数で打ち切ったりしない。相手側が authoritative な `not_found`/`missing` を返し、`can_retry_same_key` と key/payload の一致を証明した場合だけ、現在の lease/generation を保持したまま同一 key・同一 payload で retry する。それ以外は unknown のまま残す。retry の回数に恣意的な固定上限を設けて unknown を捨てる設計ではなく、未解決のままなら resource を占有する状態を可視化する。

依存 operation は predecessor の検証済み terminal receipt が確認されるまで intent を dispatch しない。dependency completion は自動 polling ではなく、root/manual caller が job completion を照合してから次の pump を呼ぶことで進む。

## Gateway job との接続

固定キーの Gateway job は既存の `home_mcp_gateway/jobs.py` の契約を再利用する。`stable_job_id` は exact idempotency key から UUID を導き（`jobs.py:51`）、explicit environment の digest は別に固定される（`jobs.py:91`）。`job_start` は `idempotency_key`、request fingerprint、cwd などを検証し（`jobs.py:236`）、同じ key の予約ディレクトリを `.runtime/jobs` で再発見する（`jobs.py:277` 以降）。queue はこの reservation/job metadata と receipts を照合し、thread の生存やタイトルだけを根拠にしない。

* Codex は PATH 上の native `codex.exe` を使います。`.cmd` wrapper ではなく native executable を指定してください。

既存の有限 `work_dispatcher` は別の queue/ledger であり、変更しない。この resource queue は `work_dispatcher` の `max_concurrency`、Claude/Codex の finite manifest、uncertain retry budget、dependency release の制限を暗黙に継承しない。両方が同じリソースを使う場合の優先順位と handover は root が明示的に決める。

## cancellation と release

cancel は cooperative stop の要求であり、force kill ではない。Gateway adapter は operation key と job ID に束縛した cancellation request file（既定では `.runtime/resource-queue/cancellations/<stable-job-id>.json`）を atomic に書くだけで、process を signal/kill しない。prepared actor/procedure がその file を poll して自分の process/children、capture、device input を止め、bound な cleanup receipt を発行する。queue/adapter はその receipt と job metadata を検証してから release する。cancel intent と terminal job response が競合した場合は、cancel の durable transaction が先に commit したとき queue の effective outcome を `cancelled` とし、実際の job の `succeeded`/`failed` は release receipt の `job_outcome` として provenance に残す。job がすでに終了していて full cleanup proof が揃う場合、二度目の cancel acknowledgment は要求しない。device input を使う procedure では以後の入力を止め、所有した入力状態を解放し、必要な画面の観測を保存する。release receipt が resource を再利用可能と証明するには、少なくとも次の各証跡を同じ operation に結び付ける。

* 自分が所有した process と children がすべて終了したこと。PID だけでなく process identity を照合する。
* capture が停止し、flush 済みで、最後のログ位置または digest が保存されていること。
* device input の送出が停止し、押下などの所有状態が解放されたこと。
* phone では `screen_state` が空でなく、actor が `screen_appropriate=true` を明示的に attestation していること。固定の `HOME` や `off` 画面を要求する契約ではなく、procedure が適切と定義した screen state を使う。

ユーザー操作と競合した場合は、追加の input を止める。ユーザーの画面を取り戻すために勝手に `HOME` などを送らず、`screen_state` と `screen_appropriate` の証拠が揃うまで release を blocked/pending とする。`HOME`/`off` への固定復帰は行わない。queue が別プロセスや外部 job を強制終了した、または hardware が安全になった、と推測する機能はない。

attempt receipt は append-only とし、既存行の update/delete で履歴を消さない。fence は期限切れ claim や再起動した owner の古い書き込みを拒否する論理的な境界であり、物理 process/device を停止する機構ではない。physical stop の証明は runner の process/capture/input/screen evidence によって別途行う。

Codex が外部停止され終了 JSONL を発行できなかった場合は、通常の `cancel` → `release --outcome cancelled` で cleanup を回復できる。bound receipt に `interrupted_recovery={requested_by, reason, stdout_sha256}` を明示する。通常の全 binding/process/capture/device 検証に加え、durable cancel、jobs metadata の `stopped`・非ゼロ exit・停止対象 root・`remaining_pids=[]`・`errors=[]`、実 stdout の digest と thread.started、一度も terminal record が無いことを要求する。inline JSONL は受理しない。元の stdout を変更せず parser の `terminal_record_invalid` を証跡に保存し、job_outcome は failed、queue outcome は cancelled とする。CLI 成功や app QA 成功の代用にはならない。

## status と将来 adapter

status は thin な local projection として adapter が必要な識別子・状態を安定して読める形にする。現在の `Queue.status()` / `task()` は local operator 向けに operation ID、operation key、job ID、`resource_id`、resource kind、dedicated thread、resource attestation hash、immutable な `device_identity`、project、input/procedure/payload と各 hash、dependencies、`review_ref`、reviewed flag と reviewed input/procedure hash、`state`/`queue_state`、`substate`、`blocker_code`、owner、generation、owner PID/create time、lease_until、last heartbeat、cancel 状態、attempts、outcome、last receipt、作成/更新時刻を返す。resource projection は kind、dedicated thread、generation、attestation hash、明示的 device identity、busy/idle、active operation/job（Gateway job ID を含む）、owner、lease_until、更新時刻を返す。将来の 41 adapter はこの中から必要な DTO を明示的に allow-list し、本文、credential、lease token、環境値、デバイス秘密情報を外部契約へ流さない。現行 local projection 自体が input/procedure/payload の hash だけに縮約されているとは扱わない。

この projection は将来の 41 adapter が query/translate できる最小 DTO を意図するが、41 の import、API、schema、DB、変更は現実装の依存ではない。41 が未接続・未導入でも local queue の transaction、pump、receipt、reconcile は成立する。adapter が後から加わっても queue の immutable rows や append-only receipts を書き換えず、observed status と root adoption を混同しない。

device evidence は準備済み actor が取得・申告した attestation であり、署名認証や独立した hardware probe は実装していない。job/worker/receipt に列挙された所有 process の PID と create time は別途照合する。今回の acceptance は fake clock/runner と local metadata の境界を検証するもので、phone/GPU の実機・推論をテストしたとは扱わない。

## 実装境界と確認

実装する範囲は、local SQLite schema/transaction、OS singleton を伴う一回の pump、immutable request builder/verifier、固定 key Gateway job への adapter、reconcile と append-only receipts、status projection である。root が明示的に呼ぶ enqueue/pump と、job completion 後に呼ぶ再 pump が最小の運用経路となる。

実装しない範囲は、常駐 daemon、idle token polling、webhook/event receiver、root/model inference の wake、phone/GPU の直接制御、外部 unqueued job の自動取り込み、force kill、別キーへの自動 fallback、41 の変更、実機の安全性や hardware result の保証である。

## one-shot CLI

実装済みの one-shot CLI は `python scripts/resource_queue.py` で起動する（module entry point は同じ `home_mcp_gateway.resource_queue_cli.main`）。これは `register`、`enqueue`、`review`、`priority`、`cancel`、`heartbeat`、`release`（`release-receipt` alias）、`receipt`（`receipts` alias）、`status`、`pump` だけを提供し、daemon/watch mode は持たない。`--queue-dir` の既定値は `.local/resource-queue`、`--runtime-dir` の既定値は既存の Gateway runtime 設定である。release は controller の OS lock 内で Gateway verifier による receipt/job/process/device evidence の検証、lease binding 照合、ledger finalize を行う。ledger の自己申告だけで release を通す bypass はない。以下の例は実際の subcommand help と source に合わせた review 用の形で、`@...` のファイルと `<...>` の値は root が準備した実値に置き換える。

```powershell
python scripts/resource_queue.py --help

# 初回 handover。attestation は空でない device_identity と state=idle/idle=true を含め、実際の端末証跡を保存する。
python scripts/resource_queue.py register phone-main phone `
  --dedicated-thread-id phone-thread-main `
  --attestation "@review/phone-main-idle.json"

# launch.json は reviewed な command/cwd/env/fingerprint/thread の immutable mapping。
python scripts/resource_queue.py enqueue op-20261024-001 phone-main phone-project `
  --input "@review/op-20261024-001-input.json" `
  --procedure "@review/op-20261024-001-procedure.json" `
  --launch "@review/op-20261024-001-launch.json" `
  --review-ref "root-review-20261024-001"

# enqueue は review_ref の有無にかかわらず blocked/review_required。input/procedure の canonical JSON SHA-256 を root の review record と突き合わせてから ready にする。
python scripts/resource_queue.py review op-20261024-001 `
  --input-hash <INPUT_SHA256> --procedure-hash <PROCEDURE_SHA256> `
  --review-ref "root-review-20261024-001" --pump

# job completion/dependency completion/unknown reconcile の後にも、root が明示的に一回呼ぶ。
python scripts/resource_queue.py pump --owner "root-manual-pump"
python scripts/resource_queue.py status op-20261024-001
python scripts/resource_queue.py receipt op-20261024-001

# cancel は request file を作るだけ。actor が停止と cleanup receipt を発行した後に release する。
python scripts/resource_queue.py cancel op-20261024-001 `
  --reason "root requested cooperative stop" --pump
python scripts/resource_queue.py release op-20261024-001 `
  --receipt "@review/op-20261024-001-release.json" --outcome cancelled --pump
```

`<INPUT_SHA256>`、`<PROCEDURE_SHA256>`、receipt の中の job/process/device identity は説明用 placeholder であり、実値なしに live operation として実行できない。CLI の `release` に retryable flag はなく、same-key retry は authoritative reconcile と controller pump の経路だけで行う。`release` は Gateway verifier を通した receipt、owner/generation、job ID、payload hash、device identity、process identity、capture、device evidence を照合し、失敗時は lease を解放しない。

`codex exec resume --help` の installed help は builder の command shape（`exec resume [SESSION_ID] [PROMPT]` と `--json`、`-c` 等）の source evidence として確認済みである。実際の新しい builder は `build_codex_resume_command`、`GatewayRunner.prepare_codex_resume`、verifier は `GatewayRunner.reconcile` / `verify_release` である。builder は native executable、`exec resume`、review 済みの option、thread、prompt、`--json` を固定し、`.ps1`/`.cmd`/`.bat` wrapper は拒否する。この help 確認は推論・モデル応答・実機の完了を示さない。

CLI の正式な command/option は引き続き runner 実装の `--help` を正本にする。acceptance が fake clock/runner で通った場合も、そのテスト名・結果・検証範囲だけを追記し、実機や推論の成功を付け足さない。

## Executor provider の移行（Codex → Claude Code）

resource の identity（`resource_id`、kind、device identity、登録時の `dedicated_thread_id`）は変えずに、専属 executor の provider/session だけを append-only の `executor_bindings` で管理する。既存 ledger は起動時に一つの `BEGIN IMMEDIATE` transaction で移行され、既存 resource は generation 1 の `codex` binding（旧 thread）を得る。既存 task は generation 1 を snapshot し、受領済み receipt・task payload・operation ID は書き換えない。移行が失敗した場合は旧 schema のまま rollback される。

* `Queue.rebind_executor(resource_id, 'claude', session_id, profile={model, effort, cwd, permission_mode}, reason=..., expected_generation=...)` / CLI `rebind-executor`。ledger 上で lease も running/unknown operation も無い idle のときだけ実行でき、`resource_busy` / `resource_dispatch_unknown` で拒否する。旧 provider/session/generation、新 provider/session/profile/generation、UTC、理由が binding 行（= migration receipt）に残り、UPDATE/DELETE は trigger で拒否される。同じ要求の再実行は何もしない。
* task は enqueue 時の binding（provider/session/generation）を immutable に snapshot し、projection の `executor` と `dedicated_thread_id` は resource の現 binding ではなく task 自身の snapshot を返す。rebind 前に queue 済みで旧 binding の task は書き換えず `executor_rebind_required`（blocked）になり、review/pump で復活しない。`cancel` して現 binding 向けに新しい operation を enqueue する。binding と異なる provider の launch は enqueue で拒否する。
* Claude の launch は `claude -p --output-format stream-json --verbose --model M --effort E --permission-mode P (--session-id | --resume) SESSION PROMPT` の固定形だけ（builder: `resource_queue_claude.build_claude_session_command`、validator: `parse_claude_command`）。初回は `--session-id`、以降は同じ session の `--resume`。`--fork-session`/`--continue`/bypass/skip-permissions/追加 option は構築も受理もしない。`P` は `default` か `acceptEdits` のみで、既定が bypass でも標準 mode を明示する（この CLI の help の choices は `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan` で `default` を列挙していないため、live では `acceptEdits` を使うか、`default` が受理されることを先に確認する）。env override は許可しない。
* task 単位の承認 tool: `prepare-claude --allowed-tools-file @list.json`（`prepare_claude_session(..., allowed_tools=[...])`）は、その task の immutable launch だけに `--allowedTools SPEC...` を追加する。resource の binding には永続化せず、list なしの task は従来と同じ argv になる。受理するのは `Bash(<明示 command>)` の完全一致 list だけで、`Bash`、`Bash(*)`、wildcard、`,;&|<>` `` ` `` `$()` や改行を含む spec、他 tool、重複、33 件以上は拒否する。各 spec は shell で連結せず argv の独立要素になり、variadic option がプロンプトを飲み込まないよう session flag の前に置く。list は argv、launch metadata（`allowed_tools` と `allowed_tools_sha256`）、payload hash に束縛され、jobs metadata の実 argv と照合され（順序違い・欠落・追加は `job_metadata_mismatch`）、parser も実 argv の list が承認 list と一致することを `launch_evidence_mismatch` で要求し、release evidence の `executor_result.allowed_tools` に残す。model/session/effort/permissionMode/fence/idempotency の検証は緩めない。
* 結果 verifier `parse_claude_stream_json` は job 自身の stdout（receipt 内の写しは使わない）を検証する。init の session、model、permissionMode、result の session/terminal/error、assistant message と `modelUsage` の model が要求と一致することを厳格に求める。effort は stream が明示した場合だけ要求と比較して不一致を拒否し、明示が無い場合は、jobs metadata と照合済みの immutable launch argv（`observed_launch_command` として明示入力）が要求 effort を含み、init の `per_turn_effort_active` が true であることを `effort_evidence`（requested / launch_argv / stream_reported / per_turn_effort_active / basis）として別々に記録して受理する。argv 証拠が無い・不一致のときは拒否する。Codex の旧 task は従来の parser と receipt 契約のまま検証され、Claude task の receipt は `executor_provider` / `executor_session_id` / `executor_generation` と `thread_id`（= session）の一致を必須とする。
* 運用手順（live は root の指示後）: `rebind-executor phone-main --session-id <uuid> --model <model> --effort xhigh --cwd <dir> --permission-mode acceptEdits --reason <text> --expected-generation 1` → `prepare-claude <operation_id> phone-main --prompt-file <file> --receipt-path <path> > launch.json`（session/model/effort/cwd/permission は binding から取り、`--session-id`/`--resume` は自動、actor envelope に current operation/job/provider/session と prior terminal job が入る）→ `enqueue ... --launch @launch.json` → `review ... --pump` → 完了後 `release` または `pump`。

## Offline acceptance（2026-10-24）

`python -m pytest tests/test_resource_queue.py tests/test_resource_queue_gateway.py -q` は 41 件 PASS。fake runner/clock/process/device と一時 ledger で、競合 claim、固定 key、応答消失、stale lease、blocked skip、priority/FIFO、cancel、release gate/rollback、再起動、空 queue、同 thread resume、phone/GPU 並列、および Gateway/CLI の結合を確認した。既存 dispatcher・generic API 境界・app-server launch/verifier の回帰 112 件も PASS。phone/GPU 実機・inference・常駐登録は実施していない。

最終受入前の fingerprint 回帰では、修正前に正式な `prepare_codex_resume` → enqueue → review → pump → CLI `_record_release` で `job_metadata_mismatch` を再現した。adapter 入口で queue 所有の `payload_hash` B に正規化し、prepared launch 内の A と B が異なる場合も raw/flattened task が同じ immutable launch になることを確認した。新しい `tests/test_resource_queue_release_integration.py` は GatewayRunner 本体と fake jobs/process seam を結合し、CLI release 後の terminal・lease 解放・次 ready dispatch、pump との一致、payload/generation/operation/job/thread/owner/key/device の receipt 改変拒否、および job metadata の fingerprint 改変拒否を確認する。既存 actor/DB、live queue、実 phone/GPU は変更・操作していない。

次の `.venv` による関連テストは **186 件 PASS**（新規結合テスト 6 件を含む）。`git diff --check` も PASS。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_resource_queue.py tests/test_resource_queue_gateway.py tests/test_resource_queue_release_integration.py tests/test_core.py tests/test_work_dispatcher.py tests/test_generic_aoitalk_boundary.py tests/test_codex_app_server_launch.py tests/test_codex_app_server_verifier.py -q
```
