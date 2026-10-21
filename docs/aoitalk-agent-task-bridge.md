# Generic AoiTalk Agent task bridge

Use the generic Agent task tools when a published AoiTalk Agent has an API-backed provider route:

- `aoitalk_agent_task_start`
- `aoitalk_agent_task_status`
- `aoitalk_agent_task_tail`
- `aoitalk_agent_task_cancel`

`start` requires the selected `agent_id` and an idempotency key. Optional `provider`, `model`, and `effort` values are expectations checked by AoiTalk against that Agent's published revision. They do not select an endpoint, credential, model fallback, or execution process. For the OpenCode IP Rotator route, pass `provider="opencode-ip-rotator"` and the exact model id configured in AoiTalk. Omit `effort` unless the AoiTalk model configuration explicitly enables that value.

AoiTalk owns the AgentRun, conversation session, provider call, tool permissions, and terminal status. The Gateway forwards the request and reuses the returned task id for status, tail, and cancellation. It does not start OpenCode, the IP Rotator proxy, or a WARP controller. It does not call a rotation endpoint or fall back to another provider.

The older `aoitalk_local_task_*` tools target the legacy local-task contract. Use the generic `aoitalk_agent_task_*` tools for the published API-backed Agent route.
