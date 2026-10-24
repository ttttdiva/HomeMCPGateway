"""One-shot local CLI for :mod:`home_mcp_gateway.resource_queue`.

The CLI intentionally has no daemon/watch mode.  A caller may request one
explicit ``pump`` after a mutation, but autonomous event wake-ups are not
implemented.  JSON arguments accept either inline JSON or a path prefixed with
``@`` (a regular existing path is accepted too), which keeps launch and
attestation schemas reviewable on disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping, Sequence

from .jobs import stable_job_id
from .resource_queue import Controller, Queue, QueueError, _operation_key
from .resource_queue_claude import CLAUDE_EFFORTS, CLAUDE_PERMISSION_MODES, CLAUDE_PROVIDER, build_actor_envelope
from .resource_queue_gateway import GatewayRunner


MAX_JSON_ARGUMENT_BYTES = 4 << 20
MAX_PROMPT_BYTES = 1 << 20


def _read_json_value(value: str | None, *, name: str, required: bool = False) -> Any:
    if value is None:
        if required:
            raise ValueError(f"{name}_required")
        return None
    raw = value
    if raw.startswith("@"):
        raw = raw[1:]
    candidate = Path(raw)
    if value.startswith("@") or candidate.exists():
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            raise ValueError(f"{name}_file_unavailable") from exc
        if len(data) > MAX_JSON_ARGUMENT_BYTES:
            raise ValueError(f"{name}_too_large")
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError(f"{name}_invalid_json") from exc
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name}_invalid_json") from exc
    if len(encoded) > MAX_JSON_ARGUMENT_BYTES:
        raise ValueError(f"{name}_too_large")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{name}_invalid_json") from exc


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resource_queue",
        description=(
            "One-shot local resource queue control. Autonomous event wake-up is not implemented; "
            "use pump explicitly or pass --pump to a supported mutation."
        ),
    )
    parser.add_argument("--queue-dir", default=os.environ.get("HOME_MCP_RESOURCE_QUEUE_DIR", ".local/resource-queue"),
                        help="queue ledger directory (default: .local/resource-queue)")
    parser.add_argument("--runtime-dir", default=None,
                        help="Gateway jobs runtime directory (default: HOME_MCP_RUNTIME_DIR/.runtime)")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="register a phone/GPU from an idle attestation")
    register.add_argument("resource_id")
    register.add_argument("kind", choices=("phone", "gpu"))
    register.add_argument("--dedicated-thread-id", "--thread-id")
    register.add_argument("--attestation", "--attestation-json", "--attestation-file", required=True,
                          help="inline JSON or @path to the initial idle attestation")
    register.add_argument("--pump", action="store_true", help="run one explicit pump after registration")

    enqueue = sub.add_parser("enqueue", help="enqueue one immutable prepared launch")
    enqueue.add_argument("operation_id")
    enqueue.add_argument("resource_id")
    enqueue.add_argument("project")
    enqueue.add_argument("--input", "--input-json", dest="input_json")
    enqueue.add_argument("--procedure", "--procedure-json", dest="procedure_json")
    enqueue.add_argument("--launch", "--launch-json", "--launch-file", dest="launch_json", required=True,
                         help="prepared launch JSON or @path")
    enqueue.add_argument("--dependency", "--depends-on", action="append", default=[])
    enqueue.add_argument("--priority", type=int, default=0)
    enqueue.add_argument("--review-ref")
    enqueue.add_argument("--pump", action="store_true", help="run one explicit pump after enqueue")

    review = sub.add_parser("review", help="pin the reviewed input/procedure hashes")
    review.add_argument("operation_id")
    review.add_argument("--input-hash", required=True)
    review.add_argument("--procedure-hash", required=True)
    review.add_argument("--review-ref")
    review.add_argument("--pump", action="store_true", help="run one explicit pump after review")

    priority = sub.add_parser("priority", help="change priority for a queued operation")
    priority.add_argument("operation_id")
    priority.add_argument("priority", type=int)
    priority.add_argument("--pump", action="store_true", help="run one explicit pump after change")

    cancel = sub.add_parser("cancel", help="request cooperative cancellation")
    cancel.add_argument("operation_id")
    cancel.add_argument("--reason", default="cancelled_by_user")
    cancel.add_argument("--pump", action="store_true", help="run one explicit pump after request")

    heartbeat = sub.add_parser("heartbeat", help="renew a matching running lease")
    heartbeat.add_argument("operation_id")
    heartbeat.add_argument("owner")
    heartbeat.add_argument("generation", type=int)

    release = sub.add_parser("release", aliases=("release-receipt",), help="verify and record one release receipt")
    release.add_argument("operation_id")
    release.add_argument("--receipt", "--receipt-json", "--receipt-file", required=True,
                         help="receipt JSON or @path")
    release.add_argument("--outcome", choices=("succeeded", "failed", "cancelled"))
    release.add_argument("--pump", action="store_true", help="run one explicit pump after release")

    rebind = sub.add_parser(
        "rebind-executor",
        help="move an idle resource to a Codex or Claude session (appends an immutable migration receipt)",
    )
    rebind.add_argument("resource_id")
    rebind.add_argument("--provider", choices=("codex", CLAUDE_PROVIDER), default=CLAUDE_PROVIDER)
    rebind.add_argument("--session-id", required=True, help="new executor session UUID (lower-case)")
    rebind.add_argument("--model", required=True, help="requested model, verified against the actual run")
    rebind.add_argument("--effort", required=True, choices=sorted(CLAUDE_EFFORTS))
    rebind.add_argument("--permission-mode", required=True, choices=sorted(CLAUDE_PERMISSION_MODES),
                        help="Claude launch mode; Codex requires default and keeps existing launch permissions")
    rebind.add_argument("--cwd", required=True, help="working directory the session lives in (resume needs the same one)")
    rebind.add_argument("--reason", required=True)
    rebind.add_argument("--expected-generation", type=int, help="current binding generation, refused if it moved")
    rebind.add_argument("--requested-by")

    prepare = sub.add_parser(
        "prepare-claude",
        help="print the immutable launch JSON for one Claude operation (feed it to enqueue --launch @file)",
    )
    prepare.add_argument("operation_id")
    prepare.add_argument("resource_id")
    prepare.add_argument("--prompt-file", required=True, help="UTF-8 text file with the operator prompt")
    prepare.add_argument("--claude-exe", default=os.environ.get("HOME_MCP_CLAUDE_EXE"),
                         help="native claude.exe (default: HOME_MCP_CLAUDE_EXE, then PATH)")
    prepare.add_argument("--mode", choices=("auto", "start", "resume"), default="auto",
                         help="auto: --session-id for the first operation of a binding, --resume afterwards")
    prepare.add_argument("--receipt-path")
    prepare.add_argument("--allowed-tools-file",
                         help="@JSON list of this task's exact Bash(<command>) approvals (default: none)")

    receipts = sub.add_parser("receipt", aliases=("receipts",), help="read durable receipts")
    receipts.add_argument("operation_id", nargs="?")

    status = sub.add_parser("status", help="read queue status")
    status.add_argument("operation_id", nargs="?")
    status.add_argument("--resource-id")

    pump = sub.add_parser("pump", help="run one controller pump")
    pump.add_argument("--owner")
    pump.add_argument("--lease-seconds", type=float, default=60.0)
    return parser


def _pump(queue: Queue, runtime_dir: str | None, owner: str | None = None, lease_seconds: float = 60.0) -> dict[str, Any]:
    runner = GatewayRunner(runtime_dir=runtime_dir)
    return Controller(queue, runner, lease_seconds=lease_seconds, owner=owner).pump()


def _prepare_claude(queue: Queue, runtime_dir: str | None, args: argparse.Namespace) -> dict[str, Any]:
    """Build the immutable launch for one Claude operation from the resource's own binding.

    Session, model, effort and working directory come from the current executor
    binding rather than from the operator, so a typo cannot start another session.
    """
    resource = queue.status(resource_id=args.resource_id)
    binding = resource["executor"]
    if binding["provider"] != CLAUDE_PROVIDER:
        raise QueueError("resource_executor_not_claude")
    claude = args.claude_exe or shutil.which("claude")
    if not claude:
        raise ValueError("claude_executable_required")
    try:
        data = Path(args.prompt_file).read_bytes()
        if len(data) > MAX_PROMPT_BYTES:
            raise ValueError("prompt_too_large")
        prompt = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("prompt_file_unavailable") from exc
    allowed_tools = _read_json_value(getattr(args, "allowed_tools_file", None), name="allowed_tools")
    if allowed_tools is None:
        allowed_tools = []
    elif not isinstance(allowed_tools, list):
        raise ValueError("allowed_tools_must_be_list")
    mode = queue.executor_launch_mode(args.resource_id) if args.mode == "auto" else args.mode
    op_key = _operation_key(args.operation_id)
    envelope = build_actor_envelope(
        operation_id=args.operation_id, job_id=stable_job_id(op_key), resource_id=args.resource_id,
        session_id=binding["session_id"], generation=binding["generation"],
        prior_terminal_job=queue.latest_terminal_job(args.resource_id),
    )
    profile = binding["profile"]
    prepared = GatewayRunner(runtime_dir=runtime_dir).prepare_claude_session(
        op_key, claude, binding["session_id"], envelope + "\n\n" + prompt, profile["cwd"],
        model=profile["model"], effort=profile["effort"], permission_mode=profile["permission_mode"], session_mode=mode,
        allowed_tools=allowed_tools, receipt_path=args.receipt_path, task_id=args.operation_id,
    )
    return prepared.as_dict()


def _record_release(
    queue: Queue,
    runtime_dir: str | None,
    operation_id: str,
    evidence: Mapping[str, Any],
    outcome: str | None,
) -> dict[str, Any]:
    """Verify and record a receipt through the Gateway adapter first."""
    if not isinstance(evidence, Mapping):
        raise QueueError("receipt_must_be_object")
    with queue.controller_lock():
        # Keep the original lease fence, verifier I/O, canonical proof check,
        # and finalize in one singleton critical section.  A generation/owner
        # change cannot race between a receipt read and release.
        task = queue.task(operation_id)
        if task is None:
            raise QueueError("operation_not_found")
        verified = GatewayRunner(runtime_dir=runtime_dir).verify_release(task, evidence)
        if not isinstance(verified, Mapping) or not verified.get("ok"):
            reason = verified.get("error") if isinstance(verified, Mapping) else None
            raise QueueError(str(reason or "release_verification_failed"))
        canonical = verified.get("release_evidence")
        if not isinstance(canonical, Mapping):
            raise QueueError("release_evidence_missing")
        selected_value = canonical.get("outcome") or verified.get("outcome")
        if not isinstance(selected_value, str) or selected_value.lower() not in {"succeeded", "failed", "cancelled"}:
            job = verified.get("job")
            thread_result = verified.get("thread_result")
            selected_value = GatewayRunner._actual_outcome(
                job if isinstance(job, Mapping) else {},
                thread_result=thread_result if isinstance(thread_result, Mapping) else None,
                receipt=evidence,
            )
        selected = selected_value.lower()
        current = queue.task(operation_id)
        if (current is None or current.get("owner") != task.get("owner")
                or current.get("generation") != task.get("generation")):
            raise QueueError("stale_release")
        effective = "cancelled" if current.get("cancel_requested") else selected
        if outcome is not None and outcome.lower() != effective:
            raise QueueError("release_outcome_mismatch")
        okay, reason = queue._validate_release_evidence(task, canonical, selected)
        if not okay:
            raise QueueError(reason)
        if not queue._finalize(task, canonical, selected):
            raise QueueError("stale_release")
        result = queue.task(operation_id)
        return result if result is not None else {"ok": True, "operation_id": operation_id}


def _run(args: argparse.Namespace) -> Any:
    queue = Queue(args.queue_dir)
    command = args.command
    if command == "register":
        result = queue.register_resource(
            args.resource_id, args.kind, args.dedicated_thread_id,
            initial_attestation=_read_json_value(args.attestation, name="attestation", required=True),
        )
        if args.pump:
            result = {"result": result, "pump": _pump(queue, args.runtime_dir)}
        return result
    if command == "enqueue":
        result = queue.enqueue(
            args.operation_id, args.resource_id, args.project,
            input=_read_json_value(args.input_json, name="input"),
            procedure=_read_json_value(args.procedure_json, name="procedure"),
            launch=_read_json_value(args.launch_json, name="launch", required=True),
            dependencies=args.dependency, priority=args.priority, review_ref=args.review_ref,
        )
        if args.pump:
            result = {"result": result, "pump": _pump(queue, args.runtime_dir)}
        return result
    if command == "review":
        result = queue.review(args.operation_id, args.input_hash, args.procedure_hash, args.review_ref)
        if args.pump:
            result = {"result": result, "pump": _pump(queue, args.runtime_dir)}
        return result
    if command == "priority":
        result = queue.set_priority(args.operation_id, args.priority)
        if args.pump:
            result = {"result": result, "pump": _pump(queue, args.runtime_dir)}
        return result
    if command == "cancel":
        result = queue.cancel(args.operation_id, args.reason)
        if args.pump:
            result = {"result": result, "pump": _pump(queue, args.runtime_dir)}
        return result
    if command == "heartbeat":
        return queue.heartbeat(args.operation_id, args.owner, args.generation)
    if command in {"release", "release-receipt"}:
        evidence = _read_json_value(args.receipt, name="receipt", required=True)
        if not isinstance(evidence, Mapping):
            raise ValueError("receipt_must_be_object")
        result = _record_release(queue, args.runtime_dir, args.operation_id, dict(evidence), args.outcome)
        if args.pump:
            result = {"result": result, "pump": _pump(queue, args.runtime_dir)}
        return result
    if command == "rebind-executor":
        return queue.rebind_executor(
            args.resource_id, args.provider, args.session_id,
            profile={"model": args.model, "effort": args.effort, "cwd": args.cwd, "permission_mode": args.permission_mode},
            reason=args.reason, expected_generation=args.expected_generation, requested_by=args.requested_by,
        )
    if command == "prepare-claude":
        return _prepare_claude(queue, args.runtime_dir, args)
    if command in {"receipt", "receipts"}:
        return {"receipts": queue.receipts(args.operation_id)}
    if command == "status":
        return queue.status(args.operation_id, resource_id=args.resource_id)
    if command == "pump":
        return _pump(queue, args.runtime_dir, owner=args.owner, lease_seconds=args.lease_seconds)
    raise ValueError("unknown_command")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        _emit(_run(args))
        return 0
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _emit({"ok": False, "error": "interrupted"})
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary intentionally safe
        code = str(exc) if isinstance(exc, (ValueError, QueueError)) and str(exc) else "queue_error"
        # Keep filesystem/provider internals out of the local JSON contract.
        if len(code) > 96 or any(char in code for char in "\\/\n\r"):
            code = "queue_error"
        _emit({"ok": False, "error": code})
        return 1


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
