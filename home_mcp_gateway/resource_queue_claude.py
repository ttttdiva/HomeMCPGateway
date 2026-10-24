"""Claude Code executor helpers for the durable resource queue.

A resource's dedicated executor may be a Claude Code print session.  This
module owns the two provider-specific pieces the queue needs and nothing else:

* a fixed-shape argv builder/validator for ``claude -p --output-format
  stream-json`` that names the session and a standard permission mode
  explicitly (``--session-id`` for the first turn, ``--resume`` for every later
  turn) and never forks, continues, or bypasses permissions; and
* a strict verifier for the complete ``stream-json`` output of one turn.

The verifier does not reuse the Codex JSONL parser and does not emit Codex
shaped records.  It reports what the stream proves (session, model, permission
mode, terminal result) and what the immutable launch argv requested (effort),
and keeps the two apart in its evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

CLAUDE_PROVIDER = "claude"
CLAUDE_ROUTE = "claude-print-session"
CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_SESSION_MODES = frozenset({"start", "resume"})
# Standard prompting modes only.  The ambient default may be a bypass, so the
# mode is always named in the argv; bypassPermissions and the skip-permissions
# flags can never be built or accepted.
CLAUDE_PERMISSION_MODES = frozenset({"default", "acceptEdits"})
MAX_STREAM_BYTES = 64 << 20
_SYNTHETIC_MODEL = "<synthetic>"
# The CLI's stream does not normally name the effort; if a stream does, it must
# agree with the request.
_EFFORT_KEYS = ("effort", "effort_level", "effortLevel", "reasoning_effort")
# argv layout of build_claude_session_command(); indexes are part of the contract.
# Optional ``--allowedTools SPEC...`` follows the permission mode and precedes the
# session flag, so the variadic option cannot swallow the trailing prompt.
_MODEL, _EFFORT, _PERMISSION = 6, 8, 10
_TOOLS_FLAG = "--allowedTools"
_SESSION_FLAGS = {"--session-id": "start", "--resume": "resume"}
_MIN_ARGV_LENGTH = 14
MAX_ALLOWED_TOOLS = 32
_MAX_TOOL_SPEC = 1024
# A spec is one explicit command: no wildcard, list separator, shell control or
# substitution character.
_FORBIDDEN_TOOL_CHARS = frozenset("*,;&|<>`$()\r\n\x00")


class ClaudeExecutorError(ValueError):
    """A stable, secret-free error code."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def canonical_session_id(value: Any) -> str:
    """Return the canonical lower-case UUID, or raise ``invalid_session_id``."""
    if not isinstance(value, str):
        raise ClaudeExecutorError("invalid_session_id")
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ClaudeExecutorError("invalid_session_id") from None
    if parsed != value:
        raise ClaudeExecutorError("invalid_session_id")
    return parsed


def _model(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > 160 or value.startswith("-")
            or any(ch.isspace() or ch == "\x00" for ch in value)):
        raise ClaudeExecutorError("invalid_model")
    return value


def _effort(value: Any) -> str:
    if not isinstance(value, str) or value not in CLAUDE_EFFORTS:
        raise ClaudeExecutorError("invalid_effort")
    return value


def _permission_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in CLAUDE_PERMISSION_MODES:
        raise ClaudeExecutorError("invalid_permission_mode")
    return value


def normalize_cwd(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ClaudeExecutorError("invalid_cwd")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ClaudeExecutorError("invalid_cwd")
    return str(path.resolve())


def normalize_profile(value: Any) -> dict[str, str]:
    """Validate the requested profile pinned by a Claude executor binding.

    ``cwd`` is part of the profile because Claude stores a session under its
    working directory; ``--resume`` only finds the session from the same one.
    """
    if not isinstance(value, Mapping) or set(value) != {"model", "effort", "cwd", "permission_mode"}:
        raise ClaudeExecutorError("invalid_executor_profile")
    return {
        "model": _model(value["model"]), "effort": _effort(value["effort"]), "cwd": normalize_cwd(value["cwd"]),
        "permission_mode": _permission_mode(value["permission_mode"]),
    }


def normalize_allowed_tools(value: Any) -> list[str]:
    """Validate a task's approved tool list: only ``Bash(<explicit command>)`` specs.

    ``Bash``, ``Bash(*)``, any wildcard, other tools and permission flags are
    refused.  An empty list is the default and adds nothing to the launch.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ClaudeExecutorError("invalid_allowed_tools")
    specs = list(value)
    if len(specs) > MAX_ALLOWED_TOOLS or len(set(map(str, specs))) != len(specs):
        raise ClaudeExecutorError("invalid_allowed_tools")
    for spec in specs:
        if (not isinstance(spec, str) or len(spec) > _MAX_TOOL_SPEC or not spec.startswith("Bash(") or not spec.endswith(")")):
            raise ClaudeExecutorError("invalid_allowed_tool")
        command = spec[len("Bash("):-1]
        if not command or command != command.strip() or _FORBIDDEN_TOOL_CHARS.intersection(command):
            raise ClaudeExecutorError("invalid_allowed_tool")
    return specs


def _executable(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ClaudeExecutorError("invalid_claude_executable")
    path = Path(value)
    if path.suffix.casefold() in {".ps1", ".cmd", ".bat"}:
        # job_start receives argv directly; a shell wrapper would pick its own
        # route and may open a console.
        raise ClaudeExecutorError("claude_executable_must_be_native")
    if not path.is_absolute() or path.name.casefold() not in {"claude.exe", "claude"}:
        raise ClaudeExecutorError("invalid_claude_executable")
    return value


def _prompt(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or value.startswith("-"):
        raise ClaudeExecutorError("invalid_prompt")
    return value


def build_claude_session_command(
    claude_executable: str,
    session_id: str,
    prompt: str,
    *,
    model: str,
    effort: str,
    permission_mode: str,
    session_mode: str,
    allowed_tools: Sequence[str] = (),
) -> list[str]:
    """Build the one supported ``claude`` print invocation.

    ``session_mode='start'`` names a new session with ``--session-id``;
    ``'resume'`` continues that same session with ``--resume``.  The permission
    mode is always explicit and limited to :data:`CLAUDE_PERMISSION_MODES`.
    ``allowed_tools`` is this one task's exact ``Bash(<command>)`` approvals (see
    :func:`normalize_allowed_tools`), each its own argv element; nothing is
    persisted on the resource.  There is no ``--fork-session``, ``--continue``,
    settings or MCP option.
    """
    if session_mode not in CLAUDE_SESSION_MODES:
        raise ClaudeExecutorError("invalid_session_mode")
    tools = normalize_allowed_tools(allowed_tools)
    return [
        _executable(claude_executable), "-p", "--output-format", "stream-json", "--verbose",
        "--model", _model(model), "--effort", _effort(effort), "--permission-mode", _permission_mode(permission_mode),
        *([_TOOLS_FLAG, *tools] if tools else []),
        "--session-id" if session_mode == "start" else "--resume", canonical_session_id(session_id),
        _prompt(prompt),
    ]


def parse_claude_command(command: Any) -> dict[str, Any]:
    """Return the bound fields of a command built by :func:`build_claude_session_command`.

    The command must equal a rebuilt command, so no extra option (fork,
    continue, permission bypass, settings or MCP override) can ride along.
    ``allowed_tools`` is the exact approved list (empty when none).
    """
    if not isinstance(command, list) or len(command) < _MIN_ARGV_LENGTH or not all(isinstance(item, str) for item in command):
        raise ClaudeExecutorError("claude_command_invalid")
    index = _PERMISSION + 1
    tools: list[str] = []
    if command[index] == _TOOLS_FLAG:
        end = next((i for i in range(index + 1, len(command)) if command[i] in _SESSION_FLAGS), None)
        if end is None:
            raise ClaudeExecutorError("claude_command_invalid")
        tools, index = command[index + 1:end], end
    mode = _SESSION_FLAGS.get(command[index])
    if mode is None or len(command) != index + 3:
        raise ClaudeExecutorError("claude_command_invalid")
    fields = {
        "executable": command[0], "model": command[_MODEL], "effort": command[_EFFORT],
        "permission_mode": command[_PERMISSION], "allowed_tools": tools, "session_id": command[index + 1],
        "session_mode": mode, "prompt": command[index + 2],
    }
    try:
        rebuilt = build_claude_session_command(
            fields["executable"], fields["session_id"], fields["prompt"], model=fields["model"],
            effort=fields["effort"], permission_mode=fields["permission_mode"], session_mode=mode,
            allowed_tools=tools,
        )
    except ClaudeExecutorError:
        raise ClaudeExecutorError("claude_command_invalid") from None
    if rebuilt != command:
        raise ClaudeExecutorError("claude_command_invalid")
    return fields


def build_actor_envelope(
    *,
    operation_id: str,
    job_id: str,
    resource_id: str,
    session_id: str,
    generation: int,
    prior_terminal_job: Mapping[str, Any] | None,
) -> str:
    """Startup envelope that keeps the actor from waiting on itself.

    ``current_actor_job_id`` is the job this very prompt runs in; the prior
    terminal job is a different, already finished job.  Both are named so the
    actor never issues ``job_wait`` on its own job.
    """
    prior = prior_terminal_job.get("job_id") if prior_terminal_job else None
    return "\n".join((
        "[resource-queue actor envelope]",
        f"current_operation_id: {operation_id}",
        f"current_actor_job_id: {job_id}  (this job; never job_wait on it)",
        f"current_resource_id: {resource_id}",
        f"current_provider: {CLAUDE_PROVIDER}",
        f"current_session_id: {session_id}",
        f"current_binding_generation: {generation}",
        f"prior_terminal_job: {prior or 'none'}  (already finished; context only)",
        "After the claim, read the current binding and store it in the release receipt. Finish with the bound "
        "release receipt, raw artifact references and a minimal result, then end the turn; leave heavy analysis to the case reviewer.",
        "[end envelope]",
    ))


def _text_of(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        return _text_of(value.get("level"))
    return None


def _effort_values(event: Mapping[str, Any]) -> list[str]:
    return [text for key in _EFFORT_KEYS if (text := _text_of(event.get(key))) is not None]


def _pending(code: str, **extra: Any) -> dict[str, Any]:
    return {"state": "result_pending", "code": code, **extra}


def parse_claude_stream_json(
    data: bytes,
    *,
    expected_session_id: str,
    expected_model: str,
    expected_effort: str,
    expected_permission_mode: str,
    observed_launch_command: Sequence[str] | None,
    expected_allowed_tools: Sequence[str] = (),
    max_bytes: int = MAX_STREAM_BYTES,
) -> dict[str, Any]:
    """Verify one complete ``--output-format stream-json`` turn.

    ``observed_launch_command`` is the argv recorded in the keyed job's own
    metadata.  The requested effort is proven only by that argv matching the
    request, so it is parsed here rather than trusted as a flag.  The result is
    ``succeeded`` or ``failed`` only when the launch argv, init event, every
    session-bearing record, the actual model, the permission mode, any effort
    the stream states, and the single terminal ``result`` record all agree with
    the request; every other case is ``result_pending`` with an exact ``code``.
    """
    if not isinstance(data, bytes) or len(data) > max_bytes:
        return _pending("result_too_large")
    try:
        session = canonical_session_id(expected_session_id)
        model = _model(expected_model)
        effort = _effort(expected_effort)
        permission = _permission_mode(expected_permission_mode)
        tools = normalize_allowed_tools(expected_allowed_tools)
    except ClaudeExecutorError as exc:
        return _pending(exc.code)
    if observed_launch_command is None:
        return _pending("launch_evidence_missing")
    try:
        launched = parse_claude_command(list(observed_launch_command))
    except (ClaudeExecutorError, TypeError):
        return _pending("launch_evidence_invalid")
    if ((launched["session_id"], launched["model"], launched["effort"], launched["permission_mode"], launched["allowed_tools"])
            != (session, model, effort, permission, tools)):
        return _pending("launch_evidence_mismatch")
    log_digest = hashlib.sha256(data).hexdigest()
    try:
        events = [json.loads(line) for line in data.decode("utf-8", errors="strict").splitlines() if line.strip()]
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _pending("invalid_jsonl")
    if not events or any(not isinstance(event, dict) for event in events):
        return _pending("empty_or_invalid_jsonl")
    inits = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
    if len(inits) != 1:
        return _pending("init_record_invalid")
    init = inits[0]
    try:
        init_session = canonical_session_id(init.get("session_id"))
    except ClaudeExecutorError:
        return _pending("session_identity_invalid")
    if init_session != session:
        return _pending("session_identity_mismatch", init_session_id=init_session)
    for event in events:
        if "session_id" in event and event["session_id"] != session:
            return _pending("session_identity_conflict")
    results = [(index, event) for index, event in enumerate(events) if event.get("type") == "result"]
    if len(results) != 1:
        return _pending("terminal_record_invalid")
    result_index, result = results[0]
    if result_index != len(events) - 1:
        return _pending("events_after_terminal")
    if result.get("session_id") != session:
        return _pending("session_identity_mismatch")

    # Actual profile.  The init model is the session's model; the result's
    # modelUsage and every assistant message must agree, and the init must
    # report the permission mode that was requested.
    if init.get("model") != model:
        return _pending("actual_model_mismatch", actual_model=init.get("model") if isinstance(init.get("model"), str) else None)
    for event in events:
        message = event.get("message") if event.get("type") == "assistant" else None
        actual = message.get("model") if isinstance(message, Mapping) else None
        if actual is not None and actual not in (model, _SYNTHETIC_MODEL):
            return _pending("actual_model_mismatch")
    usage = result.get("modelUsage")
    usage_models = set(usage) if isinstance(usage, Mapping) else set()
    if usage_models and model not in usage_models:
        return _pending("actual_model_mismatch")
    if init.get("permissionMode") != permission:
        return _pending("actual_permission_mode_mismatch",
                        actual_permission_mode=init.get("permissionMode") if isinstance(init.get("permissionMode"), str) else None)
    # Effort.  An effort the stream states must equal the request.  When it states
    # none, the proof is the metadata-verified launch argv plus the init's own
    # per_turn_effort_active observation; the two are recorded separately.
    streamed = _effort_values(init) + _effort_values(result)
    if any(value != effort for value in streamed):
        return _pending("actual_effort_mismatch", actual_effort=streamed[0])
    per_turn = init.get("per_turn_effort_active")
    if not streamed and per_turn is not True:
        return _pending("actual_effort_unverified")
    effort_evidence = {
        "requested": effort,
        "launch_argv": launched["effort"],
        "stream_reported": streamed[0] if streamed else None,
        "per_turn_effort_active": per_turn if isinstance(per_turn, bool) else None,
        "basis": "stream_reported" if streamed else "launch_argv+per_turn_effort_active",
    }

    is_error = result.get("is_error")
    subtype = result.get("subtype")
    if not isinstance(is_error, bool) or not isinstance(subtype, str):
        return _pending("terminal_record_invalid")
    denials = result.get("permission_denials")
    profile = {
        "provider": "claude", "session_id": session, "model": model, "effort": effort,
        "effort_evidence": effort_evidence, "permission_mode": permission, "allowed_tools": list(tools),
        "permission_denials": len(denials) if isinstance(denials, list) else None,
    }
    if is_error or subtype != "success":
        return {"state": "failed", "code": "turn_failed", "terminal_subtype": subtype,
                "log_digest": log_digest, "log_bytes": len(data), **profile}
    text = result.get("result")
    if not isinstance(text, str):
        return _pending("final_message_missing")
    if not usage_models:
        # A successful turn must show the model that produced it.
        return _pending("actual_model_unverified")
    result_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    evidence = {**profile, "result_digest": result_digest, "log_digest": log_digest, "log_bytes": len(data)}
    evidence_digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"state": "succeeded", "terminal_subtype": subtype, "result_text": text, "result_digest": result_digest,
            "result_chars": len(text), "log_digest": log_digest, "log_bytes": len(data),
            "evidence_digest": evidence_digest, **profile}


__all__ = [
    "CLAUDE_EFFORTS", "CLAUDE_PERMISSION_MODES", "CLAUDE_PROVIDER", "CLAUDE_ROUTE", "CLAUDE_SESSION_MODES",
    "ClaudeExecutorError", "build_actor_envelope", "build_claude_session_command", "canonical_session_id",
    "MAX_ALLOWED_TOOLS", "normalize_allowed_tools", "normalize_cwd", "normalize_profile", "parse_claude_command",
    "parse_claude_stream_json",
]
