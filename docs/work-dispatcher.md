# Work dispatcher (finite, explicitly armed)

`home_mcp_gateway/work_dispatcher.py` (entry point `scripts/work_dispatcher.py`) progresses a **finite queue of
already-authorized jobs** while the root is absent. Version 1 remains the legacy Claude-only route. Version 2 adds
explicit `aoitalk-claude` and standalone `gateway-codex` routes. It is separate from the read-only
`work_supervisor` observer, which still never creates/sends/reconnects/cancels/retries.

**Status of this document:** describes tested source. It is not evidence that anything is armed, running,
scheduled, or that a real task was submitted. No auto-start is installed; nothing survives an OS reboot
unless a root separately starts it.

## What it does / does not do

* Per job: create ONE fresh session (`aoitalk_cli_session_create`), then send ONE immutable prompt
  (`aoitalk_cli_session_send`). Version 2 `gateway-codex` instead uses one keyed D80 Gateway job to launch the
  explicitly rooted official Codex CLI entrypoint. That route does not use D41 or an AoiTalk session. No route
  reconnects, cancels, approves, falls back to another provider or creates a second scheduler.
* No work is derived from worker output, telemetry or a worker-count target. The queue ends when its N jobs end.
* It cannot preserve cloud-root model inference, nor wake a root. Results stay in the observer's outbox for the root.
* `permission_mode`, `allowed_capabilities`, `execution_profile_id` pass through exactly as the manifest supplies
  them; omitted stays omitted (the wrapper default `native_default` applies). The dispatcher selects no mode.

## Manifest (UTF-8 JSON, strict, all-or-nothing)

Fields: `version`(=1), `manifest_id`, `root_source_id`, `root_epoch`, `created_at`, `expires_at` (tz-aware ISO),
`max_concurrency` (1-8), `uncertain_retry_budget` (0-10), `jobs[]` (1-200). Each job: `job_id`, `authorization_ref`
(audit text), `create` (`agent_id`, `provider`=`claude-cli`, `model`, `working_directory` + optional `effort`, `title`,
`permission_mode`, `allowed_capabilities`, `execution_profile_id`), `message` (inline, exact), optional `resource_key`.
Unknown/duplicate keys, NaN/Infinity, bool-as-int, duplicate job IDs, non-UUID agent, lone surrogates, etc. reject the
whole manifest before any I/O. Idempotency keys are `wd1:<create|send>:<sha256(manifest_digest NUL job_id NUL op)>`.

The SHA-256 pins the bytes. It is **not** authentication of who wrote them; the CLI is a trusted operator interface and
same-user filesystem state is not a security boundary against a hostile local worker.

## Commands (`.local/work-dispatcher/` holds ledger, `status.json`, `dispatcher.log`, `dispatcher.lock`)

```
python scripts/work_dispatcher.py validate --manifest M.json [--sha256 HEX]      # offline; no ledger, no network
python scripts/work_dispatcher.py arm --manifest M.json --sha256 HEX --expires-at 2026-01-01T00:00:00Z   # imports only; sends nothing
python scripts/work_dispatcher.py once                                           # one pass
python scripts/work_dispatcher.py run [--interval 10] [--watch]                  # until drained; --watch idles for new arms
python scripts/work_dispatcher.py stop                                           # ask `run --watch` to exit
python scripts/work_dispatcher.py status | show MANIFEST_ID                      # local ledger only
python scripts/work_dispatcher.py disarm MANIFEST_ID                             # stops NEW requests; no remote cancel
python scripts/work_dispatcher.py release MANIFEST_ID JOB_ID ... --generation G               # root's review decision (see below); sends nothing
```

Dry run with placeholders (not a usable live job): a manifest whose `agent_id` is `<AGENT-UUID>` fails `validate`;
replace placeholders with root-authorized values, `validate`, then `arm`, then `run` — all explicit root actions.

## Dependencies and root-review gate (optional, same manifest only)

A job may add `depends_on` (1-32 job IDs of the SAME manifest, unique, no self/unknown/cycle) and, only together with it,
`root_review: true`. Both are validated strictly, all-or-nothing, before arm and before any network. Manifests without
these keys behave and hash exactly as before (idempotency keys and snapshot digests unchanged). Dependencies are read from
the digest-pinned manifest bytes, so nothing about them can be edited after arm.

* A dependent job gets **no create intent at all** until every predecessor is `succeeded` (own session+run and a
  digest-verified handoff, as written by `reconcile`). Predecessor failed/cancelled/denied/uncertain stay visible as
  `dep_failed` / `dep_cancelled` / `dep_denied` / `dep_uncertain` (and `dep_upstream_failed` transitively); running
  (`dep_running`) or result-pending (`dep_result_pending`) predecessors are `waiting`, as is a predecessor whose own current create/send
  operation is unresolved-uncertain (lost response, or retry budget spent): `waiting` with code `dep_uncertain`, never a
  satisfied or failed dependency, no new key, no auto release. A failed predecessor's resource key being released never advances a dependent.
  Independent jobs continue under the usual slot/`resource_key` rules.
* `succeeded` proves execution finished, **not** that the content is adopted. Where adoption matters use `root_review`:
  the job stays queued (`root_review_wait`) until the root records a decision:

```
python scripts/work_dispatcher.py release MANIFEST_ID JOB_ID --manifest-sha256 HEX --root-epoch N --generation G \
    --predecessor PRED_JOB=SESSION_ID:RUN_ID:RESULT_SHA256 [--predecessor ...]
```

  `--generation G` is mandatory: the arm generation the root has confirmed (shown by `show`), checked for exact equality with
  the ledger's current value inside the transaction (`generation_mismatch` otherwise, nothing written; there is no fallback to
  the current value). A command saved from an earlier arm is therefore refused after a disarm/re-arm and is never
  auto-applied to the new generation. The decision binds manifest digest, root epoch, that arm generation and the exact session/run/result digest of every
  predecessor, so a different run/digest/epoch, an older or newer generation, or an incomplete set is refused or inert.
  It requires a live lease (a release after disarm/expiry is refused, and a dead lease sends nothing anyway), stores an
  immutable row (an identical repeat is a no-op, a differing one is `decision_conflict`; triggers block update/delete), and
  must be repeated after a re-arm. It sends, arms and extends nothing and is not an approval path for native/AutoReview
  refusals. Worker text, terminal success, mtimes or output counts never open a gate.
* The dependency/release state is re-checked immediately before every POST (`dependency_gate` blocks otherwise).
* `status` shows `dependency_wait`, `root_review_wait`, `dependency_failed` separately from `queued`/`expired`, per-job
  `dependency` {state, code}, and `fully_verified_succeeded`; a queue with waiting or review-pending jobs is not complete.
* Neither the gate nor `release` wakes the root or writes the observer ledger/outbox; the root still learns of results through
  the existing observer outbox.

## `run --watch` (continuous monitoring of approved jobs)

`run` still exits when drained. `run --watch` is opt-in: it stays alive and re-reads the ledger every `--interval`,
so a new, explicitly armed manifest (full strict validation, expected SHA-256, finite expiry, epoch check, stable
idempotency keys, sticky denials) is picked up without restarting. With an empty queue, an expired or disarmed arm,
or a run waiting for approval it sends nothing and just sleeps. It never arms, re-arms, extends, edits a manifest,
calls a model, or changes permissions. Stop with Ctrl+C or `work_dispatcher.py stop` (local stop file, honored
between passes; a stale one is cleared on start). After a restart the existing ledger is used: uncertain
operations stay uncertain and follow the same-arm retry rules.

This is continued monitoring/delivery of already-approved jobs. It is **not** a mechanism that starts or wakes
the root's own inference, and it does not create new work. Starting it as a daemon or scheduled task is a separate root action.

## Manifest v2: explicit standalone Codex route

The default Codex route below remains exec. A separately opt-in, default-off
stdio app-server route and its offline-only verification boundary are documented
in [Codex app-server steering](codex-app-server.md).

Version 1 parsing, operation keys, snapshots, concurrency limit (8), and Claude wrapper request shape remain unchanged.
Version 2 keeps the finite top-level fields, requires an explicit route on every job, and allows a manifest-selected
`max_concurrency` from 1 through 16. The setting counts only jobs owned by this manifest; manually launched or other
unowned jobs do not consume these dispatcher slots and are not globally capped by them.

* `route: "aoitalk-claude"` uses the existing `create` object and message contract (`provider` must be `claude-cli`).
  It is still sent only through the AoiTalk CLI session wrappers.
* `route: "gateway-codex"` uses `model`, `effort`, `node_executable`, `codex_entrypoint`, `message`, and
  `authorization_ref`. The two executable paths must be absolute canonical files; the entrypoint must be under the
  official `node_modules/@openai/codex/bin/codex.js` path. The command is generated as an argv list with `shell=False`
  and the Gateway repository as cwd. The manifest cannot supply arbitrary args, shell text, or environment values.
  The complete prompt is kept in argv and the manifest is rejected if its Windows command line would exceed the
  CreateProcess bound. No package install, model substitution, resume, approval bypass, or permission/config edit is
  performed by this route.

Before the existing jobs worker can Popen the CLI, it reopens this exact dispatcher ledger in read-only/query-only
mode and verifies the live manifest digest, root epoch, explicit generation, operation, request JSON digest, pinned
job snapshot, actual worker command/cwd, and keyed metadata fingerprint. A closed lease records `not_launched`; a
missing or contradictory identity stays unknown. The worker consumes a persistent one-shot fence before this check.
The stable Gateway UUID is derived from the exact Codex operation idempotency key by the shared jobs codec. A repeated
key reads that reservation and never starts a second worker, including after an ambiguous crash window.

Codex `running`, `result_pending`, `unknown`, failed, and verified success are separate from Claude observer results.
Dispatcher success still requires exact Gateway metadata, a confirmed exited/zero process, and the complete bounded stdout JSONL to contain one own `thread.started`,
the final completed `agent_message`, and a successful terminal `turn.completed`. The observer independently discovers matching manual and dispatcher Gateway jobs from the D80 runtime and may preserve a verified completed turn while the process metadata still lingers as running; it records process status, exit code, and execution status separately, and this does not relax dispatcher success, release a dispatcher resource slot, or change a dispatcher ledger row. Its handoff provenance exposes the Gateway job/thread/final-item IDs and digests through observer `show`. A verified
execution is not a root adoption decision. Cross-route dependencies require verified predecessor execution, and a
`root_review` release binds the manifest digest, epoch, explicit generation, and each predecessor's actual route
identity/result digest. Release sends nothing and does not approve native/AutoReview denials.

The root selects the finite route mix. There is no task generator or provider quota filler. A fake 8-Claude + 2-Codex
manifest verifies ten occupied owned slots under `max_concurrency: 10`; it makes no claim about unrelated manual work.

Existing `run --watch` processes do not hot-reload code. Before arming a v2 manifest, the root must normally stop and
verify the old watch process, then launch the reviewed code. This implementation does not replace a live watch process.

## Guarantees and limits

* **Intent before I/O:** the key, exact request and arm generation are committed first; the receipt is committed
  after. Restart resumes from the ledger, never from memory. Claimed at most: idempotent API effects under the
  existing service contract, not exactly-once distributed execution.
* **Uncertain** (transport/5xx, `submission_uncertain`) outcomes are replayed only with the identical key/payload,
  within the same arm generation, with persisted attempts (1 + budget) and backoff. Exhausted stays unresolved and
  keeps its slot. A new arm lease does not replay an older arm's uncertain request.
* **Refusals are sticky** (denied/forbidden/auth/idempotency conflict/binding mismatch/…): no fallback, re-key or
  resurrection by re-arming. Binding-invalid receipts (non-UUID session, mismatched agent/model/cwd, reused session)
  block the job; no adoption by list/title.
* **Disarm/expiry** is checked immediately before every POST, and prevents unstarted create/send and uncertain
  replay. It cannot retract a request already initiated and does not cancel accepted work. A heartbeat from the
  root neither extends nor invalidates the lease; nothing extends or re-arms it automatically.
* **Last check before every POST:** after the intent/attempt commit and immediately before the backend call, lease,
  clock, revocation, the manifest-pinned job snapshot and the op payload are re-verified (a tampered ledger row blocks
  as `snapshot_corrupt`). A request initiated after that point still cannot be retracted.
* The same guard also runs inside the wrapper, after its login and immediately before its single request (the dispatcher
  wraps `cli._session` only for the duration of one call). Session UUIDs are stored lower-case; a second receipt for the
  same session in any case blocks as `session_reused`. The op's request JSON is recompared with the manifest-pinned
  compile before every POST, including after a restart.
* **Slots:** a refusal after an attempt whose outcome was never recorded (crash/lost response) keeps the slot.
  `resource_key` is exclusive across all manifests in the ledger; no cwd/repo exclusion is invented.
  A send receipt contradicting the create binding (agent/model/cwd/mode/profile) quarantines the job; no resend.
* **Slots (cont.):** `max_concurrency` counts queue-owned unresolved jobs (create/send intents, sent-not-terminal,
  uncertain, and blocked-with-possible-execution). Jobs sharing a `resource_key` serialize; nothing else is implied.
* **Completion:** observer ledger opened `mode=ro` (never migrated/written); success = the job's own session+run is
  `succeeded` AND exactly one digest-verified handoff exists for that run, with state ready/acked, source
  `cli_output_chunks`/`message_get`, matching message ID, and a non-stalled session cursor. If the send receipt has no run ID the job stays unresolved
  (`run_unbound`); no run is inferred from a session having a single run. Top-level and nested `session` identity fields in a
  receipt must agree, otherwise the job is quarantined. Missing/busy/incompatible
  observer = result-pending. Failed/cancelled = failed. waiting/disconnected stay visible; no approval/reconnect.
* **Status** separates queued, accepted, active, uncertain, blocked, expired, succeeded (verified), failed,
  result_pending; no "root alive" or running-worker count. Logs/status carry safe codes and IDs only; the full
  prompt exists only in the private ledger.
* Observer result preservation uses the existing `work_supervisor` commit transaction to add a handoff and outbox row for the root to drain. Saving that result is not root receipt, adoption, task creation, continued inference, or a dispatcher action; the observer does not wake the root.

## Verification

`python -m unittest tests.test_work_dispatcher tests.test_work_supervisor tests.test_aoitalk_cli_sessions tests.test_jobs` (repo `.venv`).
Tests use a fake idempotent backend and injected clock/faults; they prove module behavior only — not a running
daemon, an armed queue, a real task, or reboot recovery.
