# Work supervisor（observer + durable result inbox）

`scripts/work_supervisor.py` は AoiTalk の CLI session / task と Gateway job を**観測するだけ**の常駐監視で、完全な結果を SQLite に保全します。agent の create / send / reconnect / cancel / retry、root の起動、webhook・通知先登録、秘密鍵・token・権限の追加は一切行いません。発行できるHTTPは `work_supervisor.READ_ONLY_ROUTES` に固定され（パラメータ形状も固定）、それ以外は `route_not_allowed` で拒否されます。認証とsanitizeは既存の `CliSessionClient` / `TaskClient` を再利用し、`.env` の値は読み出し・表示しません。

**できないこと（正確な範囲）**: この daemon は結果・根拠・alert を ledger / `status.json` に残すだけです。root（常時推論する主体）を起動・再開・延命する実装は**ありません**。Windows daemon から既存 cloud task を起こせる認可済み credential / 通知 callback、現在の root を起動する API は確認されておらず、架空の経路・秘密 token・無限自己起動・ユーザーPCの直接タスク経路では埋めていません。つまり「root 常時起動」は**未実装**です。root 不在中も完全な結果・未処理 alert・ready handoff が失われず、root 復帰時に未転送分を即座に取り出せる（下記 drain）ところまでが完成範囲です。同じユーザーのログオン時に observer だけを復帰させる scheduled task は実装済みです（下記「ログオン時の自動復帰」）。ただし OS の実再起動後の復帰は未試験で、root model の自動起動はありません。

## コマンド

```
python scripts/work_supervisor.py run [--interval 10]   # 10秒ごとにpoll（OS singleton lock）
python scripts/work_supervisor.py once                  # 1回poll（runが動作中なら already_running）
python scripts/work_supervisor.py status                # ledgerのみから status.json を再生成（通信なし）
python scripts/work_supervisor.py pending [--cursor N] [--limit 20]      # 読むだけ。未転送handoffのpage
python scripts/work_supervisor.py show <handoff_id>                      # 読むだけ。全文・digest・配送状態
python scripts/work_supervisor.py receive ID=SHA256 [...] --root-source-id S --root-epoch N [--note ..]  # rootが明示
python scripts/work_supervisor.py ack handoff:<id> [inbox:<n> ...] [--note ...]                          # rootが明示
python scripts/work_supervisor.py heartbeat --source-id ID --source-type root --root-epoch N --ttl 300 [--aux-count K]
```

## root 復帰時の drain 手順（root 自身が実行する。worker は代行しない）

1. root 自身が native 状態を確認し、fresh な heartbeat を記録: `heartbeat --source-type root --source-id <root> --root-epoch <新epoch> --ttl 300`。monitor が root heartbeat を書くことはありません。
2. `pending --limit 20` を `has_more=false` になるまで `--cursor <next_cursor>` で全ページ取得。`total_pending` / `remaining_after_cursor` / `has_more` を確認（省略された出力を完了扱いにしない）。`blockers`（未解決alert）と `source_freshness`（snapshot鮮度・直近poll失敗・root heartbeat）も同時に返ります。`pending` は何も受領・処理済みにしません。
3. 各 handoff を `show <handoff_id>` で全文取得し、`result_digest`（`digest_verified=true`）で内容を確認。
4. 受領を記録: `receive <handoff_id>=<result_digest> ... --root-source-id <root> --root-epoch <epoch>`。digest が一致しない / epoch が過去 / 対象が superseded なら全件失敗（all-or-nothing）。**受領は処理済みではありません**。
5. root が判断・必要な指示/報告を実行。作業を再発行する前に、必ず `status` / run の現在状態（`runs`・`queued_followups`）を照合して二重実行しない。
6. 処理した handoff を明示 ack: `ack handoff:<id>`。受領記録のない handoff の ack は `delivery_not_received` で拒否されます。ack はその handoff が消費した `cli.output` 行だけが対象で、他の run や後着の結果を巻き込まず、session を retire しません。

未受領・未処理の handoff は root 不在・heartbeat 失効・時間経過では削除されません。

## 保存先（git管理外）

`.local/work-supervisor/state.sqlite3`（WAL）に tasks / sessions(cursor) / inbox `UNIQUE(session_id,event_id)` / runs / run_lists / recovered_messages / handoffs / **outbox** / source_heartbeats / acknowledgments / snapshots を保存し、`status.json` を tmp→`os.replace` で原子的に出力します。

## 保証と限界

* fetch中はDBへ書かず、events・next_cursor・run状態・handoff+outbox・snapshotを**1 transaction**で保存します。fetch〜commit間のcrashや同一pageの再処理でも inbox / handoff / outbox 挿入は exactly-once（人間の処理が exactly-once という意味ではありません）。結果の ready 化と outbox 行は同一 transaction です。
* **実 backend の `cli.output` chunk 契約**（41 `_bounded_text_chunk`）: chunk i は `[start,end)`（Python code point）、`text_end` は**その chunk の `end`**（全文の末尾ではない）。検証は、index 0..count-1 の完全性、一貫した count、先頭0・隙間/重複/逆転なしの start/end、`len(text)==end-start`、各 chunk の `text_end==end`、`text_truncated==false`。同じ index で範囲・count・本文が異なる重複は `chunk_conflict`（上書きしない）。1つでも欠けると UNKNOWN で、理由コード付き `result_provenance_incomplete`（backend 応答の credential 風文字列が `[redacted]` に置換されて範囲と長さが合わなくなった場合は `chunk_text_redacted`）。通常chunkは4096文字なので全文が12000文字を超える前提は置きません。
* **telemetry は出力ではない**: 結果になり得るのは `cli.output`（message-bound chunk）だけ。`cli.assistant_text` / `tool_start` / `tool_end`（statusが completed でも run の完了ではない）/ `invocation` / `status_update` は診断として保存され、結果破損の alert も出しません。`assistant_message_id` の無い `cli.output`、terminal event が指す message に chunk が無い（pointer のみ）、terminal なのに結果なし、は引き続き警告します。
* **run 単位の handoff**: key は `cli:<session_id>:<run_id>:<assistant_message_id>`。run 自身の terminal 権威（run GET の `succeeded/failed/cancelled`、または自 run の terminal event / terminal status の `cli.output`）と、検証済みの全文が揃った時だけ作ります。同 session の新しい run が queued/running でも古い run の完了済み結果を隠さず、逆に新しい run の状態を継承もしません。run GET が後から active を示し terminal event と矛盾する場合は `run_state_conflict` で保留します。
* **known-run 照合**（既存の認証と ACL、read-only のみ）: session の一覧は最新 run の projection しか返しません。そこで既知 session の `GET /api/conversations/{sid}/agent-runs?limit=200&status=running|queued` と、既知 run ID の `GET /api/agent-runs/{id}?include_events=false&include_tool_calls=false&include_edges=false&include_timeline=false` で実状態を取得します（session/run の binding を検証、不一致は `binding_mismatch`）。run ID は `runs` テーブルに永続化し、1 poll あたり run 20 件・session 10 件までを least-recently-checked 順に回します。403/404/不正応答は unknown のまま 1 時間 back off し、DB 直読や別認証へは逃げません。list の上限到達・clip・不正項目は lower bound で `exact=false`。
* **結果回収**: run が `succeeded` かつ `provider_output_persisted` で events に出力が無い場合、30秒の猶予後に `GET /api/conversations/{sid}/messages?limit&since`（since は run の作成時刻の 60 秒前）で回収します。message の `id` / `session_id` / `role=assistant` / `metadata.run_id` が一致し、削除・clip・応答 truncation・bounded page 外でないことを確認できた時だけ handoff になります（検証不能なら `output_pointer_unrecovered` 等の警告で、pointer のみを本文回収済みにしません）。指数 back-off・最大30回。messages API は `cli._sanitize`（12000文字/フィールド、秘密風文字列の置換）を通るため、これを超える/書き換わった本文は検証不能として扱います。
* **minClaude**: `session_projection`（session list の最新status件数）と `reconciled_runs`（running / queued / other_active / unknown）を分けて報告します。`running` は 120 秒以内に実証した running run のみ（queued は非計上）。`exact=true` は、全 active list source が complete・error なし・lower_bound なし・fresh、snapshot が fresh、直近 poll が失敗していない、対象 session の run list が fresh で complete、unknown run が 0、projection が running なのに running run が無い session が無い、の全てを満たす時だけ。そうでない時は下限値＋`deficit_is_upper_bound`、観測が無ければ `UNKNOWN`。`not_exact_reasons` に理由を出します。範囲は「active と projection された session ＋ 既知 run ID」で、terminal と projection された session 内の未知 run は列挙しません（`scope` に明記）。target 8 に合わせた数の補完はしません。
* **応答予算**: `cli._sanitize` の 64KB 予算を list 応答全体に適用すると約 4〜5 session で切れ、件数が常に lower bound になるため、list 系 GET（`/api/cli-sessions`、tasks、agent-runs）は項目単位で sanitize します（redaction は同じ、項目が clip されれば `output_truncated`）。terminal 側の list は 1 件約 14KB で `limit=200` が 2MB 上限を超える（`server_error`）ため `limit=100`（上限到達は lower bound）。collect の応答が予算で切れて page が欠けた場合は `page_missing` と断定せず、group/limit を縮めて再取得します。
* **履歴の取り込み**: terminal で未 drain の session は 1 poll 10 件まで least-recently-collected 順に回し（`sessions.collected_at`）、active / 異常な session は毎回 collect します。これまで見えなかった過去の完了 session も発見・保全されるため、導入直後は過去分の handoff が `pending` に多数入ります（root は `pending` の digest を使って `receive` / `ack` を一括指定できます）。`cli_collect` は未読履歴が残る間 `lower_bound`（incomplete）です。
* **page / source 完備性**: `collect` の per-session `error`、要求した page の欠落、不正な events / event identity / cursor は成功 page として扱わず、cursor も `drained` も進めません（`collect_error:<code>` を項目単位で警告）。truncate は page limit を 100→25→5→1 に縮めて再試行し、回復しなければ `cursor_stalled_truncated_page`（`reason`付き）。source は `cli_list:<state>` 等の state 別に complete / lower_bound / error / observed_at を保存します。
* **cycle failure**: 直近 poll の失敗 code は `poll_cycle`（`state`, `last_failure`）と `poll_cycle_failed:<code>` alert として status にも出し、snapshot の古さ（`snapshot_age_seconds` / `snapshot_fresh`、`observer_stale`）と区別します。失敗時に古い count を fresh/exact に更新しません。
* 静寂・process消失は完了ではなく `state_unconfirmed_absent_from_lists`。`/api/agent-tasks` は個別GETのみで、一覧は未確認のため観測対象外です。
* aux=6 は root が `heartbeat --aux-count` で明示した値のみ使用し、無ければ `UNKNOWN`。heartbeat は source_id / source_type / observed_at / expires_at / root_epoch を記録し（暗号署名は無し）、monitor が自動更新・延命することはありません。root_epochの巻き戻りは拒否します。target Claude 8 / aux 6 は目標値で、観測値ではありません。
* 失敗は固定のsafe error codeだけを保存します。

## ledger の移行

ledger は v4 です（v2: run/message単位handoff、v3: 保存済みeventの再分類、v4: runs / outbox / 結果回収）。旧版のDBは初回接続時に `state.sqlite3.pre-v4-<ts>.bak` へ丸ごとバックアップしてから、原子的な1 transaction で移行し、行は削除しません。v1の session 単位 handoff は `superseded_v1`（outbox も `superseded`、復活しない）、`ready` は outbox `pending`、v1/v3 で ack 済みは outbox `received`（`legacy_ack_implies_receipt`）になります。v1のackで立った `retired` は解除され、旧 ack は新しい run の handoff に影響しません。**スキーマを変えるため、旧 daemon を止めてから新コードで接続してください**（旧 daemon と新スキーマの同時稼働は避ける）。

## ログオン時の自動復帰（observer のみ）

`scripts/work_supervisor_autostart.ps1 -Action <plan|install|status|disable|enable|start|stop|uninstall|selftest>`（既定の TaskName は `Home MCP Work Supervisor`。既存の `Home MCP Gateway` task / `install_autostart.ps1` は変更しません）。

* 定義: 現在のユーザー SID、Interactive token、RunLevel Limited、同ユーザーの AtLogOn、MultipleInstances IgnoreNew、Action は `.venv\Scripts\pythonw.exe "<repo>\scripts\work_supervisor.py" run`（cwd は repo）。password / S4U / SYSTEM / Highest / RunAs は使わず、credential は task にも新規ファイルにも保存しません（observer が実行時に既存の AoiTalk settings を読む）。昇格・ExecutionPolicy 変更・別認証経路は使いません。
* 同名 task が既にあり、Action の exe / 引数 / cwd / principal SID / logon / 設定が**厳密に一致しない**場合は `BLOCKED`（操作・対象・理由・現状態を JSON で出力、exit 2）で、上書きも停止もしません。登録が拒否された場合も `BLOCKED`（exit 2）で、回避しません。
* 二重起動の防止は scheduler の IgnoreNew だけに頼りません。`run` / `once` は **singleton lock を connect()（migration）より先に取る**ため、重複起動は台帳を開かず、`already_running` をログに残して exit 0（scheduler が即再起動しない）。`start` は既に observer が動いていれば何もしません。
* ログ: `.local/work-supervisor/supervisor.log`（1MiB × 現行 + 5 世代）。start / stop / exit の safe code、cycle 失敗 code（変化時のみ）、復旧、30 cycle ごとの鮮度と pending 件数のみ。識別子形式以外の値は `redacted`。本文・.env・credential・例外メッセージ・HTTP body は書きません。pythonw は stdout が無いので失敗もこのログで追えます。
* 停止 / 再開:
  * 停止（次回ログオンでも起動しない）: `-Action disable`（Disable → 自 task の Stop）。一時停止だけなら `-Action stop`。
  * 再開: `-Action enable` の後 `-Action start`。
  * 解除: `-Action uninstall`（task だけ除去。台帳・outbox・receipt・ack・status.json・ログは残す）。
* 未実証: Windows 本体の再起動 / ログオフ後の実際の自動起動（OS reboot の実試験は未実施）、ユーザー未ログオン時・OS 起動前の稼働（Interactive task のため対象外）。root model の常時起動・再開はできません。
