# Codex app-server steering (offline implementation)

This feature is **default off**. Source and fake stdio tests do not establish live
readiness. No live Codex runtime, authentication, service restart, or configuration
change is part of this implementation.

`HOME_MCP_CODEX_APP_SERVER_ENABLED=1` must be explicitly enabled in the dispatcher
and worker launch environment before a new dispatcher-bound job can use
`transport: "codex_app_server"`. An omitted transport keeps the existing exec
route. Running exec jobs cannot be steered or migrated.

## Opt-in request

The version 2 manifest retains its existing top-level fields. A synthetic job
entry looks like this; executable paths and the model must be the actual values
selected by the root before a separately authorized live test:

```json
{
  "job_id": "synthetic-steer-smoke",
  "route": "gateway-codex",
  "authorization_ref": "root:separately-authorized-smoke",
  "transport": "codex_app_server",
  "model": "ROOT_SELECTED_MODEL",
  "effort": "high",
  "node_executable": "C:\\ROOT_SELECTED_INSTALL\\node.exe",
  "codex_entrypoint": "C:\\ROOT_SELECTED_INSTALL\\node_modules\\@openai\\codex\\bin\\codex.js",
  "message": "Synthetic input: inspect this repository without changing any files."
}
```

The dispatcher derives and pins the complete `app_server` spec in its immutable
`create_request`:

```json
{
  "model": "ROOT_SELECTED_MODEL",
  "model_provider": "openai",
  "effort": "high",
  "input_text": "Synthetic input: inspect this repository without changing any files.",
  "cwd": "C:\\path\\to\\HomeMCPGateway",
  "sandbox": "read-only",
  "approval_policy": "never",
  "approvals_reviewer": "user"
}
```

The internal launch uses `job_start` with the existing exact dispatcher binding,
stable operation key and dispatcher request fingerprint, plus
`transport="codex_app_server"` and the full `app_server` object above. The command
is exactly `[validated_node, validated_official_entrypoint, "app-server"]` and
the cwd is the pinned Gateway repository. A public caller cannot omit the
dispatcher binding and use `job_start` to start an arbitrary app-server.
An optional manifest `app_server` object must exactly equal the derived spec.

For completeness, this is the exact extended `job_start` body shape used by the
dispatcher. Every binding/key/digest below is synthetic and must be replaced by
the dispatcher's own committed values; hand-written placeholders cannot pass the
guard:

```json
{
  "command": ["C:\\ROOT_SELECTED_INSTALL\\node.exe", "C:\\ROOT_SELECTED_INSTALL\\node_modules\\@openai\\codex\\bin\\codex.js", "app-server"],
  "cwd": "C:\\path\\to\\HomeMCPGateway",
  "idempotency_key": "wd2:codex_start:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "request_fingerprint": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "dispatcher_binding": {
    "db_path": "C:\\path\\to\\HomeMCPGateway\\.local\\work-dispatcher\\state.sqlite3",
    "manifest_id": "synthetic-steer-manifest",
    "manifest_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "root_source_id": "synthetic-root",
    "root_epoch": 1,
    "generation": 1,
    "job_id": "synthetic-steer-smoke",
    "op_key": "wd2:codex_start:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "request_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "gateway_job_id": "11111111-1111-4111-8111-111111111111",
    "request_fingerprint": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "transport": "codex_app_server",
  "app_server": {
    "model": "ROOT_SELECTED_MODEL",
    "model_provider": "openai",
    "effort": "high",
    "input_text": "Synthetic input: inspect this repository without changing any files.",
    "cwd": "C:\\path\\to\\HomeMCPGateway",
    "sandbox": "read-only",
    "approval_policy": "never",
    "approvals_reviewer": "user"
  }
}
```

Only OpenAI, read-only, approval `never`, and reviewer `user` are accepted in this
initial scope. These are explicit restrictions of this transport; the source
does not establish that the old exec route resolved to equivalent permissions.
Model and effort remain selected by the dispatcher. Arbitrary RPCs, config maps,
turn overrides, tools, plugin options and login options are not accepted.

## Tools and receipts

The two new tools have these exact argument bodies:

```json
{
  "job_id": "11111111-1111-4111-8111-111111111111",
  "expected_turn_id": "TURN_ID_FROM_THIS_JOB_STATUS",
  "text": "Synthetic steering: focus the answer on the offline test boundary.",
  "idempotency_key": "synthetic-steer-001"
}
```

```json
{
  "job_id": "11111111-1111-4111-8111-111111111111",
  "idempotency_key": "synthetic-steer-001"
}
```

Their discovery schemas have `type: "object"` and
`additionalProperties: false`. For `job_steer`, the properties and required keys
are exactly `job_id`, `expected_turn_id`, `text`, and `idempotency_key`, all
strings. For `job_steer_status`, they are exactly `job_id` and `idempotency_key`,
both strings. UTF-8 byte limits are enforced by the implementation.

`job_steer` accepts a nonempty UTF-8 text of at most 32 KiB and an idempotency key
of at most 255 characters. At most eight requests may remain pending. Neither
tool accepts a runtime directory, filesystem path, command, model, sandbox, tools,
or an arbitrary protocol `UserInput` object. Both MCP schemas use
`additionalProperties: false`, and runtime argument validation rejects extra keys.
The Gateway's existing authentication
boundary and the exact dispatcher binding/control identity are the authorization
scope. There is no new per-user ownership or ACL system.

The Gateway atomically publishes immutable, job-local requests. The worker alone
writes delivery state and evidence. Reusing a key with the same body returns the
same receipt identity; changing the body conflicts. Receipt reads remain separate
from submit admission and validate the job, binding and metadata integrity.
Public receipt fields do not include the text, credentials or private handles.

| State or evidence | Meaning |
| --- | --- |
| `queued` | Published locally; delivery to the runtime is not established. |
| `sending` | A durable send intent exists. While the connection remains alive, a delayed reply stays correlated to this RPC. |
| `accepted_by_runtime` | A successful native `turn/steer` reply returned the expected turn ID. |
| `rejected` | The request was refused; the fixed reason describes the failure. |
| `not_delivered` | Derived when no send record exists and the mailbox is closed or its worker is gone. |
| `delivery_uncertain` | Derived when a send intent exists but its connection or worker is lost before a durable outcome. |
| `recorded` evidence | An actual native `userMessage.clientId` exactly matched this request's generated `clientUserMessageId`. |

Acceptance does not establish that the model used the input. There is no `applied`
or `consumed` state. Uncertain requests are never automatically resent. A later
receipt can reflect the original RPC's late reply without issuing a new RPC.
Exec jobs return `steer_unsupported`; jobs without an active turn return
`no_active_turn`. A turn-ending race never starts another turn or another job.

Submit checks run at Gateway admission and again immediately before the worker
writes the steer RPC. They verify manifest bytes/digest/generation, root epoch,
route, execution and exact operation/spec identity. The running steer guard
ignores the startup `arm_expires_at` lease. Manifest revocation, expiry, a newer
root epoch, stop and the mailbox close fence prevent new sends.

## Protocol and result evidence

One existing worker owns one stdio child, one reader and a writer lock. The
adapter initializes with `clientInfo` only. It starts a thread with every pinned
policy field and exactly one generated config key, `model_reasoning_effort`.
The returned model, provider, canonical cwd, sandbox, approval, reviewer and
non-null effort must match before the adapter may send `turn/start`.

Every server request receives its method-specific denial or JSON-RPC error.
Approval requests under `never` produce `policy_unexpected_request` evidence.
Authentication refresh and attestation requests fail closed; the adapter does
not log in or fall back to exec. Existing DPAPI startup environment transfer is
used without copying credentials into spec, metadata or RPC parameters.

`appserver.rpc.jsonl` is the private protocol journal. It holds correlated native
send/receive envelopes, while authentication request content is redacted.
`stdout.log` is only a readable public-tail projection and is not result proof.
The journal is bounded to 64 MiB and a frame to 1 MiB; exceeding either fails
closed with `appserver_evidence_limit`.

Dispatcher and observer select the dedicated verifier using the pinned
transport. A result requires the target thread/turn's native `turn/completed` and
the last completed target `agentMessage` before it. Other turns, proactive
messages and partial deltas do not supply the final answer. Failed and interrupted
turns cannot succeed. The evidence digest covers the pinned spec, native terminal
proof, final answer digest, and ordered steer digests, delivery states and exact
recording evidence. An uncertain send sets `input_set_uncertain=true`.

Turn outcome and child shutdown are separate facts. After a normal terminal
turn, the worker closes stdin, allows two seconds, and uses the existing identity
checked process-tree termination if needed. `appserver_shutdown_forced` does not
turn a valid completed turn into legacy `nonzero_exit`. A child crash before a
terminal turn is not success. A child whose worker is gone is `unknown` with
`control_lost` and cannot be steered.

`job_stop` attempts native `turn/interrupt`, records the RPC acknowledgement
separately from the native interrupted terminal event, and verifies process-tree
termination. Its single monotonic deadline reserves two seconds of the default
ten-second budget for tree termination. Worker-loss fallback retains exact PID
and process creation-time verification.

## Remaining live checks

A separately authorized root smoke test must establish actual `turn/steer`
acceptance, real `userMessage.clientId` events, Windows app-server stdin EOF
behavior, authentication-mode server requests, policy resolution and provider
behavior. Fake fixtures do not establish any of these. Visibility of native
request-user-input, MCP and plugins also remains unverified.

The minimal next entry is one fresh version 2 dispatcher job with the transport
above, the explicitly enabled launch environment, an actual selected model and
validated installed paths. Poll that job for its active turn ID, submit one
synthetic `job_steer` body, and read the matching `job_steer_status` receipt. Any
live execution, enabling/configuration and restart needed to load this source
require a separate instruction; none is performed by this offline task.
