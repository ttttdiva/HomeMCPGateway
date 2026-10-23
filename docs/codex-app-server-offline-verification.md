# Offline verification guide

These targeted fixtures exercise Codex app-server transport, steering, result
verification and compatibility using a Python fake stdio server. The feature is
default off; enable it only in isolated test environments for these fixtures.
Offline tests alone do not establish live provider acceptance; run live
validation only under explicit authorization.

## Targeted tests

The following selection includes the transport fixtures and related legacy
regressions. It is not the full repository suite.

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

Runtime fixtures launch the Python fake server, rather than the installed Codex
executable. Cross-process mailbox publishers and fake descendants use hidden
Windows subprocess flags. Integration fixtures pass a real adapter journal to
the dedicated verifier, so the proof path includes the parser and verifier
together. Launch fixtures mock the scheduler bootstrap.

Method-specific denial validation covers command execution, file change,
permissions, v1 apply patch, v1 exec command and MCP elicitation. These protocol
response shapes can be checked offline without a provider or app-server process.

## Fixture matrix

| Required fixture | Fixture location |
| --- | --- |
| 1. Native handshake, policy check, target answer and terminal success | `test_codex_app_server_runtime.py`, `test_codex_app_server_integration.py` |
| 2. Every effective policy field missing/mismatched; zero turn starts | Runtime policy subcases; strict verifier policy tests |
| 3. Acceptance separate from exact clientId recording | Runtime, verifier and cross-module integration tests |
| 4. Turn-ending/no-active race; no replacement turn | Runtime terminal/error/final-writer guard tests; public active-turn checks |
| 5. Actual fake pipe loss after send, uncertainty and zero restart resend | Runtime `steer_crash` fixture and exclusive journal/worker fences |
| 6. Idempotent key/body, conflict, wrong job, exec, injection, revoked gate, expired arm | Mailbox/API, launch/guard and MCP schema tests |
| 7. All server request families and unknown method | Fake server request tests and method-specific denial validation |
| 8. Partial publication, exclusive close fence, worker loss, cap eight, stopping | Mailbox cross-process and runtime final-writer race tests |
| 9. Delayed reply correlation; no timer uncertainty; ordered frames | Runtime late reply and writer tests; verifier and receipt tests |
| 10. Forced shutdown after completion; preterminal crash; control loss | Runtime, verifier, dispatcher and jobs identity fixtures |
| 11. Interrupt acknowledgement separate from interrupted terminal; shared stop budget | Runtime stop/deadline/descendant tests; mocked shared-deadline helper test |
| 12. Legacy exec identity/parser and dispatcher/observer ordering/dedup/outbox | Fixed-hash compatibility test and the focused legacy targets above |
| 13. Pinned metadata/spec, safe projections, provenance digest differences | Genuine bound dispatcher/supervisor fixtures; API/schema/verifier tests |

Compatibility fixtures assert fixed `make_argv` and `parse_codex_jsonl` source
hashes and the historical canonical request shape. The fixed synthetic legacy
keyed fingerprint is
`cb78ebb9788f41c4367ae6b6ba515a70fc6a978aadf9da0ccc5f2256f13627b9`.

## Boundaries and live validation

Fake-stdio behavior cannot establish actual app-server acceptance of
`turn/steer`, real emitted `userMessage.clientId`, Windows app-server stdin EOF
behavior, native pipe backpressure, authentication server-request behavior,
provider/model policy resolution, or visibility of request-user-input, MCP and
plugins. Network and live provider validation are separate from these fixtures.

Offline test success does not authorize login, token refresh, feature or
environment configuration changes, service restarts, unrelated job operations,
or live tasks. Existing DPAPI environment transport remains the credential
boundary; credentials must not be copied into public specs or fixtures.

See [the transport contract and exact synthetic bodies](codex-app-server.md) for
the opt-in entry, tool schemas, receipt meanings, result verifier and minimal
live-validation entry. Run live validation only under explicit authorization.
