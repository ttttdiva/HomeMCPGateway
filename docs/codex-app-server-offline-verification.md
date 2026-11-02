# Offline acceptance record

Scope: D80 source, fake stdio fixtures, focused regressions, commit and normal
push. This record is not live Codex acceptance. The feature remains default off;
the ambient environment was checked and reported `feature_enabled=False`.
Only isolated test environments enable the feature.

## Verification

The final combined focused run passed **148 tests in 41.91 seconds**: 101 checks
in the new transport/compatibility fixtures and 47 selected existing regressions.
An additional three existing observer ledger-migration tests passed in 0.59
seconds. `git diff --check` passed.

The selected targets are:

```powershell
$offlineTargets = @(
  'tests/test_codex_app_server_integration.py',
  'tests/test_codex_app_server_launch.py',
  'tests/test_codex_app_server_runtime.py',
  'tests/test_codex_app_server_verifier.py',
  'tests/test_job_steering.py',
  'tests/test_job_steer_schema.py',
  'tests/test_work_dispatcher.py::CodexRouteTests',
  'tests/test_work_supervisor.py::GatewayRecovery',
  'tests/test_work_supervisor.py::CrashAndReplay',
  'tests/test_jobs.py::JobTests::test_pid_reuse_does_not_signal_other_process',
  'tests/test_jobs.py::JobTests::test_worker_final_metadata_is_reread_after_process_exit',
  'tests/test_jobs.py::JobTests::test_missing_worker_without_final_metadata_remains_lost',
  'tests/test_jobs.py::JobTests::test_metadata_read_retries_transient_sharing_violation',
  'tests/test_jobs.py::JobTests::test_metadata_read_persistent_permission_error_is_not_hidden',
  'tests/test_jobs.py::JobTests::test_keyed_empty_reservation_stays_unknown_without_launch'
)
& .\.venv\Scripts\python.exe -m pytest -q @offlineTargets
& .\.venv\Scripts\python.exe -m pytest -q tests/test_work_supervisor.py::GatewayLedgerMigration tests/test_work_supervisor.py::LedgerMigration
```

This is a targeted selection, not the repository's full suite. Runtime tests
launch a Python fake stdio server, never the installed Codex executable.
Cross-process mailbox publishers and fake descendants run with hidden Windows
subprocess flags. The real adapter journal is also passed to the real dedicated
verifier in integration tests; the proof path is not tested only with a mocked
parser.

The following six denial results were independently validated against the saved
installed 0.163.0 response schemas using `jsonschema`: command execution, file
change, permissions, v1 apply patch, v1 exec command, and MCP elicitation. All six
passed. No provider or app-server process is needed for that check.

| Required fixture | Evidence location |
| --- | --- |
| 1. Native handshake, policy check, target answer and terminal success | `test_codex_app_server_runtime.py`, `test_codex_app_server_integration.py` |
| 2. Every effective policy field missing/mismatched; zero turn starts | Runtime policy subcases; strict verifier policy tests |
| 3. Acceptance separate from exact clientId recording | Runtime, verifier and cross-module integration tests |
| 4. Turn-ending/no-active race; no replacement turn | Runtime terminal/error/final-writer guard tests; public active-turn checks |
| 5. Actual fake pipe loss after send, uncertainty and zero restart resend | Runtime `steer_crash` fixture and exclusive journal/worker fences |
| 6. Idempotent key/body, conflict, wrong job, exec, injection, revoked gate, expired arm | Mailbox/API, launch/guard and MCP schema tests |
| 7. All server request families and unknown method | Fake server request tests plus six saved-schema checks |
| 8. Partial publication, exclusive close fence, worker loss, cap eight, stopping | Mailbox cross-process and runtime final-writer race tests |
| 9. Delayed reply correlation; no timer uncertainty; ordered frames | Runtime late reply and writer tests; verifier and receipt tests |
| 10. Forced shutdown after completion; preterminal crash; control loss | Runtime, verifier, dispatcher and jobs identity fixtures |
| 11. Interrupt acknowledgement separate from interrupted terminal; shared stop budget | Runtime stop/deadline/descendant tests; mocked shared-deadline helper test |
| 12. Legacy exec identity/parser and dispatcher/observer ordering/dedup/outbox | Fixed-hash compatibility test and the focused legacy targets above |
| 13. Pinned metadata/spec, safe projections, provenance digest differences | Genuine bound dispatcher/supervisor fixtures; API/schema/verifier tests |

`make_argv` and `parse_codex_jsonl` source hashes were captured before edits and
are asserted unchanged. The fixed synthetic legacy keyed fingerprint remains
`cb78ebb9788f41c4367ae6b6ba515a70fc6a978aadf9da0ccc5f2256f13627b9`.
The compatibility test also checks the historical canonical request shape.

Two independent source reviews reproduced and led to fixes for a final-writer
stop race, late fatal protocol evidence overriding a completed turn, premature
observer handoff from an open journal, and immutable transport/spec checks.

## Test side effects and boundaries

An earlier separate execution of the existing `test_jobs.py` module passed 15
tests. Those tests created temporary Python jobs through the existing Windows
TaskScheduler bootstrap, and teardown stopped only their own temporary job IDs.
The worker's existing bootstrap removes its temporary launch task. This was not
a live Codex test. The final combined selection above avoids that scheduler
bootstrap; new launch fixtures mock it.

No real Codex app-server, Thread, Turn or provider test was performed. No login,
token refresh, authentication operation, feature/environment configuration
change, D80/D41 restart, or stop of an unrelated job was performed. Existing DPAPI
environment transport was read in source and reused unchanged. Untracked
`repo.local/`, `stdout`, and unrelated concurrent changes are excluded from the
delivery.

## Live work remains unverified

Actual app-server acceptance of `turn/steer`, real emitted `userMessage.clientId`,
Windows app-server stdin EOF behavior, native pipe backpressure, authentication
server-request behavior, provider/model policy resolution, and visibility of
request-user-input/MCP/plugins remain unverified. A Python fake's behavior is not
evidence for these live properties.

See [the transport contract and exact synthetic bodies](codex-app-server.md) for
the opt-in entry, tool schemas, receipt meanings, result verifier, and minimal
next smoke-test entry. Any live execution requires a separate instruction.
