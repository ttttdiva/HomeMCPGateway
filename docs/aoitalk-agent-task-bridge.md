# AoiTalk API Agent task bridge

The five `aoitalk_agent_task_*` API Agent tools are exposed only through the dedicated AoiTalk MCP connection. Use them when a published AoiTalk Agent has an API-backed provider route:

- `aoitalk_agent_task_start`
- `aoitalk_agent_task_status`
- `aoitalk_agent_task_tail`
- `aoitalk_agent_task_result`
- `aoitalk_agent_task_cancel`

`start` requires the selected `agent_id` and an idempotency key. Optional `provider`, `model`, and `effort` values are expectations checked by AoiTalk against that Agent's published revision. They do not select an endpoint, credential, model fallback, or execution process. For the OpenCode IP Rotator route, pass `provider="opencode-ip-rotator"` and the exact model id configured in AoiTalk. Omit `effort` unless the AoiTalk model configuration explicitly enables that value.

AoiTalk owns the AgentRun, conversation session, provider call, tool permissions, and terminal status. The Gateway forwards the request and reuses the returned task id for status, tail, and cancellation. It does not start OpenCode, the IP Rotator proxy, or a WARP controller. It does not call a rotation endpoint or fall back to another provider.

The older `aoitalk_local_task_*` tools target the legacy local-task contract. Use the generic `aoitalk_agent_task_*` tools for the published API-backed Agent route.

## Retrieving implementation answers

Status replies preserve CRLF, newlines, tabs and Unicode inside `result`.
They remain previews: strings over 4,000 characters or replies over the 64 KiB
JSON budget set `output_truncated=true`. Short UI and error display fields keep
their existing single-line behavior. Use `aoitalk_agent_task_result` to retrieve
a longer implementation answer without losing indentation:

1. Call `aoitalk_agent_task_result(task_id, offset=0, limit=4000)`.
2. Save `content`, `content_sha256`, `total_chars` and `total_bytes`.
3. While `next_offset` is not null, call the same tool with that offset and
   `expected_digest=content_sha256`, appending each `content` in order.
4. Verify the joined string's character count, UTF-8 byte count and SHA-256.
   Preserve the returned text exactly when writing it to a file; do not normalize
   line endings. The digest describes the redacted text, not the upstream original.

Offsets and limits count Unicode code points (Python characters), not bytes or
UTF-16 code units. The default limit is 4,000, the maximum is 8,000. An empty
answer or offset exactly at the end returns empty content and `next_offset=null`.
An offset past the end is rejected. `output_truncated` is false only when the
page contains the entire answer; a continuation page still sets it true even
when `next_offset=null`. No characters are silently dropped between pages.

This read-only tool uses the existing authenticated task GET endpoint. It checks
the returned task ID and accepts only `completed`/`succeeded` tasks with a string
`result.response` (or the older `result.final_response`). It never returns tail
events, progress, tool output, failure answers, prompts or reasoning fields.
Credentials and token patterns are redacted across the **whole answer before
any slicing**, including secrets spanning page boundaries. CRLF/newline/tab
controls are preserved; other characters below space are removed as before.

Each request re-reads the task without a local cache. Nonzero offsets require
`expected_digest`; `result_changed` means discard the partial reconstruction
and restart at zero. Non-successful tasks return `result_not_ready`; missing
final answers return `result_unavailable`. Malformed responses and upstream
bodies over 2 MiB are rejected, never silently adopted. Every returned page is
bounded by the 64 KiB JSON response budget. This change needs no new upstream
endpoint; the dedicated AoiTalk MCP server must be updated separately before
the new tool is available in its live catalog.

## Authentication reuse and Retry-After

While the Gateway lifespan is active, the task, Agent-task and CLI-session adapters share one in-memory,
event-loop-local authenticated transport per validated endpoint/username/password (`aoitalk_auth.py`).
Initial login is single-flight; cookies are never written to disk and reads stay fresh API calls. A changed
configuration, a 401 from the API, or Gateway shutdown drops and closes the client. Operations are never
replayed after an error. A login 429 stores its validated `Retry-After` (seconds or HTTP-date, at most 24 h)
as a monotonic per-endpoint/user cooldown; callers inside it get `rate_limited` with `retry_after_seconds`
and no new login. An API (non-login) 429 starts the same cooldown, so following calls make no request until it expires. A missing or malformed header yields `rate_limited` without `retry_after_seconds` and holds a 60 s Gateway-internal cooldown, reported as `local_cooldown_seconds` (never as the server-advertised `retry_after_seconds`).
Outside the lifespan (tests, other loops) and in the `aoitalk_clip_*` tools, each call still logs in once;
those tools report `retry_after_seconds` but do not share a session.
