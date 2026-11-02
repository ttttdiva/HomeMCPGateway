"""Private stdio adapter for the fixed Grok marker smoke operation.

Only operation identity metadata and the prompt hash reach the existing
SQLite OperationLedger. Authentication and gateway traffic stay in Node.
"""

from __future__ import annotations

from datetime import datetime
import json
import hashlib
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, TextIO
import uuid

from ._grok_bot_recovery import (
    AccountMismatchError,
    OperationBindingMismatchError,
    OperationAnchorError,
    OperationCorrelationError,
    OperationConflictError,
    OperationLedger,
    TeamMismatchError,
)


OPERATION_ID = "GROK-MARKER-20261024-1652"
PROMPT = (
    "Reply with exactly GROK-HARNESS-20261024-1652-OK. "
    "Do not use tools, files, commands, or external services."
)
_ALLOWED_STATES = {"accepted", "unknown", "rejected"}
_RECORD_STATES = {"prepared", "sending", "accepted", "unknown", "rejected"}
_BASE_RECORD_COLUMNS = (
    "operation_id",
    "account_identity",
    "team_identity",
    "client_nonce",
    "target_identity",
    "prompt_sha256",
    "transport_state",
)
_CORRELATION_COLUMNS = ("request_message_id", "request_id", "root_id")
_TASK_STATE_BASE_KEYS = {"action", "operation_id", "state"}
_TASK_STATE_CORRELATED_KEYS = _TASK_STATE_BASE_KEYS | {
    "account_identity",
    "team_identity",
    "target_identity",
    "client_nonce",
    "prompt_sha256",
    *_CORRELATION_COLUMNS,
}
_DIAGNOSTIC_PHASES = {"send_receipt", "request_lookup"}
_DIAGNOSTIC_RESPONSE_TYPES = {
    "object", "array", "null", "string", "number", "boolean", "undefined",
}
_DIAGNOSTIC_REASONS = {
    "RECEIPT_NOT_OBJECT",
    "RECEIPT_MESSAGE_ID_MISSING",
    "RECEIPT_MESSAGE_ID_CONFLICT",
    "RECEIPT_CONTEXT_MISMATCH",
    "RECEIPT_REQUEST_ID_MISSING",
    "RECEIPT_REQUEST_ID_CONFLICT",
    "RECEIPT_ROOT_ID_MISSING",
    "RECEIPT_ROOT_ID_CONFLICT",
    "REQUEST_NONCE_NOT_FOUND",
    "REQUEST_NONCE_AMBIGUOUS",
    "REQUEST_CONTEXT_MISMATCH",
    "REQUEST_MESSAGE_ID_UNOBSERVED",
    "REQUEST_ROOT_ID_MISSING",
    "REQUEST_ID_UNOBSERVED",
}
_DIAGNOSTIC_KEY_ALLOWLIST = {
    "accountIdentity", "account_identity", "agentId", "agent_id", "clientNonce",
    "client_nonce", "content", "entries", "id", "kind", "message", "messageId",
    "message_id", "requestId", "request_id", "requestMessageId", "request_message_id",
    "rootId", "root_id", "role", "sender", "targetId", "target_id", "targetIdentity",
    "target_identity", "teamIdentity", "team_identity", "text", "type",
}
_DIAGNOSTIC_REQUIRED_KEYS = {
    "phase", "reason", "response_type", "present_keys", "unknown_key_count",
    "http_status", "observed_at",
}
_MAX_DIAGNOSTIC_BYTES = 4096
_MAX_DIAGNOSTICS = 64
_MAX_UNKNOWN_DIAGNOSTIC_KEYS = 1_000_000
_DIAGNOSTIC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_MAX_INPUT_BYTES = 64 * 1024


def default_ledger_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".runtime" / "grok-bot-marker-operations.sqlite3"


class MarkerLedgerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _validate_diagnostic(value: Any) -> dict[str, Any]:
    optional_keys = {"tail_entries", "nonce_matches"}
    if not isinstance(value, dict):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    keys = set(value)
    if not _DIAGNOSTIC_REQUIRED_KEYS.issubset(keys) or keys - (
        _DIAGNOSTIC_REQUIRED_KEYS | optional_keys
    ):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    if (
        not isinstance(value["phase"], str)
        or value["phase"] not in _DIAGNOSTIC_PHASES
        or not isinstance(value["reason"], str)
        or value["reason"] not in _DIAGNOSTIC_REASONS
        or not isinstance(value["response_type"], str)
        or value["response_type"] not in _DIAGNOSTIC_RESPONSE_TYPES
        or not isinstance(value["present_keys"], list)
        or len(value["present_keys"]) > len(_DIAGNOSTIC_KEY_ALLOWLIST)
        or any(
            not isinstance(key, str) or key not in _DIAGNOSTIC_KEY_ALLOWLIST
            for key in value["present_keys"]
        )
        or value["present_keys"] != sorted(set(value["present_keys"]))
        or isinstance(value["unknown_key_count"], bool)
        or not isinstance(value["unknown_key_count"], int)
        or not 0 <= value["unknown_key_count"] <= _MAX_UNKNOWN_DIAGNOSTIC_KEYS
        or not (
            value["http_status"] is None
            or (
                isinstance(value["http_status"], int)
                and not isinstance(value["http_status"], bool)
                and 100 <= value["http_status"] <= 599
            )
        )
        or not isinstance(value["observed_at"], str)
        or not _DIAGNOSTIC_TIMESTAMP.fullmatch(value["observed_at"])
    ):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    try:
        datetime.strptime(value["observed_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise MarkerLedgerError("OPERATION_RECORD_INVALID") from None
    for key in optional_keys:
        if key in value and (
            isinstance(value[key], bool)
            or not isinstance(value[key], int)
            or not 0 <= value[key] <= 40
        ):
            raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    if value["phase"] != "request_lookup" and keys & optional_keys:
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    if "nonce_matches" in value and "tail_entries" in value and (
        value["nonce_matches"] > value["tail_entries"]
    ):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID") from None
    if len(encoded) > _MAX_DIAGNOSTIC_BYTES:
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    return value


def _ensure_diagnostic_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS operation_diagnostics ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "operation_id TEXT NOT NULL, diagnostic_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS operation_diagnostics_operation_id_id "
        "ON operation_diagnostics(operation_id, id)"
    )


def _read_task_diagnostics(ledger_path: str | Path, operation_id: str) -> list[dict[str, Any]]:
    ledger_file = Path(ledger_path).resolve()
    try:
        with sqlite3.connect(ledger_file.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_diagnostics'"
            ).fetchone()
            if table is None:
                return []
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(operation_diagnostics)")
            }
            if not {"id", "operation_id", "diagnostic_json"}.issubset(columns):
                raise MarkerLedgerError("LEDGER_SCHEMA_INVALID")
            rows = connection.execute(
                "SELECT diagnostic_json FROM operation_diagnostics "
                "WHERE operation_id=? ORDER BY id DESC LIMIT ?",
                (operation_id, _MAX_DIAGNOSTICS),
            ).fetchall()
    except MarkerLedgerError:
        raise
    except sqlite3.Error:
        raise MarkerLedgerError("LEDGER_READ_FAILED") from None
    diagnostics = []
    for row in reversed(rows):
        try:
            diagnostic = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            raise MarkerLedgerError("OPERATION_RECORD_INVALID") from None
        diagnostics.append(_validate_diagnostic(diagnostic))
    return diagnostics


def _valid_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 1024
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _prompt_sha256() -> str:
    return hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()


def _existing_record(
    path: str | Path,
    operation_id: str = OPERATION_ID,
    expected_prompt_sha256: str | None = None,
) -> dict[str, Any]:
    if not _valid_identifier(operation_id):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    if operation_id == OPERATION_ID and expected_prompt_sha256 is None:
        expected_prompt_sha256 = _prompt_sha256()
    ledger_file = Path(path).resolve()
    if not ledger_file.is_file():
        raise MarkerLedgerError("OPERATION_NOT_FOUND")
    try:
        with sqlite3.connect(ledger_file.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(operations)")}
            required = {
                *_BASE_RECORD_COLUMNS,
            }
            if not required.issubset(columns):
                raise MarkerLedgerError("LEDGER_SCHEMA_INVALID")
            correlation_columns = set(_CORRELATION_COLUMNS)
            if columns & correlation_columns and not correlation_columns.issubset(columns):
                raise MarkerLedgerError("LEDGER_SCHEMA_INVALID")
            selected_columns = (
                _BASE_RECORD_COLUMNS
                + _CORRELATION_COLUMNS
                if correlation_columns.issubset(columns)
                else _BASE_RECORD_COLUMNS
            )
            row = connection.execute(
                "SELECT " + ",".join(selected_columns) + " "
                "FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
    except MarkerLedgerError:
        raise
    except sqlite3.Error as error:
        if isinstance(error, sqlite3.OperationalError) and "no such table" in str(error).lower():
            raise MarkerLedgerError("LEDGER_SCHEMA_INVALID") from None
        raise MarkerLedgerError("LEDGER_READ_FAILED") from None
    if row is None:
        raise MarkerLedgerError("OPERATION_NOT_FOUND")

    result = {key: row[key] for key in row.keys()}
    if (
        result["operation_id"] != operation_id
        or not _valid_identifier(result["account_identity"])
        or not _valid_identifier(result["team_identity"])
        or not _valid_identifier(result["target_identity"])
        or not isinstance(result["prompt_sha256"], str)
        or len(result["prompt_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in result["prompt_sha256"])
        or (expected_prompt_sha256 is not None and result["prompt_sha256"] != expected_prompt_sha256)
        or result["transport_state"] not in _RECORD_STATES
    ):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    try:
        parsed_nonce = uuid.UUID(result["client_nonce"])
    except (ValueError, TypeError, AttributeError):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID") from None
    if parsed_nonce.version != 4 or str(parsed_nonce) != result["client_nonce"].lower():
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    correlation_values = tuple(result.get(key) for key in _CORRELATION_COLUMNS)
    if any(value is not None for value in correlation_values):
        if not all(value is not None for value in correlation_values):
            raise MarkerLedgerError("OPERATION_RECORD_INVALID")
        if any(not _valid_identifier(value) for value in correlation_values):
            raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    else:
        for key in _CORRELATION_COLUMNS:
            result.pop(key, None)
    return result


def _confirm_existing_record(payload: dict[str, Any], path: str | Path) -> dict[str, str]:
    required_keys = {
        "action",
        "operation_id",
        "account_identity",
        "team_identity",
        "target_identity",
        "client_nonce",
        "prompt_sha256",
        "request_message_id",
        "request_id",
        "root_id",
    }
    if set(payload) != required_keys:
        raise ValueError("invalid_request")
    if (
        payload["operation_id"] != OPERATION_ID
        or payload["prompt_sha256"] != _prompt_sha256()
        or any(not _valid_identifier(payload[key]) for key in (
            "account_identity", "team_identity", "target_identity",
            "client_nonce", "request_message_id", "root_id",
        ))
        or (payload["request_id"] is not None and not _valid_identifier(payload["request_id"]))
    ):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")
    try:
        parsed_nonce = uuid.UUID(payload["client_nonce"])
    except (ValueError, TypeError, AttributeError):
        raise MarkerLedgerError("OPERATION_RECORD_INVALID") from None
    if parsed_nonce.version != 4 or str(parsed_nonce) != payload["client_nonce"].lower():
        raise MarkerLedgerError("OPERATION_RECORD_INVALID")

    ledger_file = Path(path).resolve()
    if not ledger_file.is_file():
        raise MarkerLedgerError("OPERATION_NOT_FOUND")
    try:
        connection = sqlite3.connect(
            ledger_file.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT operation_id,account_identity,team_identity,client_nonce,target_identity,prompt_sha256,transport_state "
                "FROM operations WHERE operation_id = ?",
                (OPERATION_ID,),
            ).fetchone()
            if row is None:
                raise MarkerLedgerError("OPERATION_NOT_FOUND")
            for key in (
                "operation_id", "account_identity", "team_identity", "client_nonce",
                "target_identity", "prompt_sha256",
            ):
                if row[key] != payload[key]:
                    raise MarkerLedgerError("OPERATION_BINDING_MISMATCH")
            if row["transport_state"] not in _RECORD_STATES:
                raise MarkerLedgerError("OPERATION_RECORD_INVALID")
            connection.execute(
                "UPDATE operations SET transport_state='accepted' WHERE operation_id=?",
                (OPERATION_ID,),
            )
            connection.execute("COMMIT")
            return {"operation_id": OPERATION_ID, "transport_state": "accepted"}
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
    except MarkerLedgerError:
        raise
    except sqlite3.Error:
        raise MarkerLedgerError("LEDGER_WRITE_FAILED") from None


def dispatch(payload: Any, ledger_path: str | Path) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
        raise ValueError("invalid_request")
    action = payload["action"]

    if action == "read":
        if set(payload) != {"action", "operation_id"} or payload["operation_id"] != OPERATION_ID:
            raise ValueError("invalid_request")
        return {"ok": True, **_existing_record(ledger_path)}

    if action == "anchor":
        if set(payload) != {"action", "operation_id"} or payload["operation_id"] != OPERATION_ID:
            raise ValueError("invalid_request")
        record = _existing_record(ledger_path, expected_prompt_sha256=_prompt_sha256())
        if record["transport_state"] != "accepted":
            raise MarkerLedgerError("MARKER_ANCHOR_NOT_ACCEPTED")
        return {"ok": True, **record}

    if action == "task_read":
        if set(payload) != {"action", "operation_id"}:
            raise ValueError("invalid_request")
        operation_id = payload["operation_id"]
        if not _valid_identifier(operation_id) or operation_id == OPERATION_ID:
            raise ValueError("invalid_request")
        return {
            "ok": True,
            **_existing_record(ledger_path, operation_id),
            "diagnostics": _read_task_diagnostics(ledger_path, operation_id),
        }

    if action == "task_begin":
        if set(payload) != {
            "action", "operation_id", "account_identity", "team_identity",
            "target_identity", "prompt",
        }:
            raise ValueError("invalid_request")
        operation_id = payload["operation_id"]
        prompt = payload["prompt"]
        if (
            not _valid_identifier(operation_id)
            or operation_id == OPERATION_ID
            or not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.encode("utf-8")) > 32 * 1024
        ):
            raise ValueError("invalid_request")
        anchor = _existing_record(
            ledger_path,
            expected_prompt_sha256=_prompt_sha256(),
        )
        if anchor["transport_state"] != "accepted":
            raise MarkerLedgerError("MARKER_ANCHOR_NOT_ACCEPTED")
        ledger = OperationLedger(ledger_path)
        try:
            record, created = ledger.begin_send(
                operation_id=operation_id,
                account_identity=payload["account_identity"],
                team_identity=payload["team_identity"],
                target_identity=payload["target_identity"],
                prompt=prompt,
                required_anchor_operation_id=OPERATION_ID,
                required_anchor_prompt_sha256=_prompt_sha256(),
            )
        except OperationAnchorError as error:
            raise MarkerLedgerError(str(error)) from None
        return {
            "ok": True,
            "created": created,
            "operation_id": record.operation_id,
            "account_identity": record.account_identity,
            "team_identity": record.team_identity,
            "target_identity": record.target_identity,
            "prompt_sha256": record.prompt_sha256,
            "client_nonce": record.client_nonce,
            "transport_state": record.transport_state,
            "diagnostics": _read_task_diagnostics(ledger_path, record.operation_id),
        }

    if action == "task_state":
        payload_keys = set(payload)
        has_diagnostic = "diagnostic" in payload_keys
        state_keys = payload_keys - {"diagnostic"}
        if state_keys == _TASK_STATE_BASE_KEYS:
            correlation = None
        elif state_keys == _TASK_STATE_CORRELATED_KEYS:
            correlation = {
                key: payload[key]
                for key in _TASK_STATE_CORRELATED_KEYS
                if key not in _TASK_STATE_BASE_KEYS
            }
        else:
            if has_diagnostic or payload_keys & (
                _TASK_STATE_CORRELATED_KEYS - _TASK_STATE_BASE_KEYS
            ):
                raise MarkerLedgerError("OPERATION_RECORD_INVALID")
            raise ValueError("invalid_request")
        diagnostic = _validate_diagnostic(payload["diagnostic"]) if has_diagnostic else None
        operation_id = payload["operation_id"]
        if (
            not _valid_identifier(operation_id)
            or operation_id == OPERATION_ID
            or not isinstance(payload["state"], str)
            or payload["state"] not in _ALLOWED_STATES
        ):
            raise ValueError("invalid_request")
        if operation_id == OPERATION_ID:
            raise ValueError("invalid_request")
        _existing_record(ledger_path, operation_id)
        if correlation is not None:
            if any(not _valid_identifier(value) for value in correlation.values()):
                raise MarkerLedgerError("OPERATION_RECORD_INVALID")
            try:
                record = OperationLedger(ledger_path).set_transport_state(
                    operation_id,
                    payload["state"],
                    correlation=correlation,
                )
            except OperationBindingMismatchError:
                raise MarkerLedgerError("OPERATION_BINDING_MISMATCH") from None
            except OperationCorrelationError:
                raise MarkerLedgerError("OPERATION_RECORD_INVALID") from None
        else:
            record = OperationLedger(ledger_path).set_transport_state(
                operation_id, payload["state"]
            )
        if diagnostic is not None:
            ledger_file = Path(ledger_path).resolve()
            try:
                with sqlite3.connect(
                    ledger_file.as_uri() + "?mode=rw", uri=True, timeout=5
                ) as connection:
                    _ensure_diagnostic_table(connection)
                    connection.execute(
                        "INSERT INTO operation_diagnostics(operation_id, diagnostic_json) "
                        "VALUES (?, ?)",
                        (
                            operation_id,
                            json.dumps(
                                diagnostic, ensure_ascii=False, separators=(",", ":")
                            ),
                        ),
                    )
                    connection.execute(
                        "DELETE FROM operation_diagnostics WHERE operation_id=? AND id NOT IN ("
                        "SELECT id FROM operation_diagnostics WHERE operation_id=? "
                        "ORDER BY id DESC LIMIT ?) ",
                        (operation_id, operation_id, _MAX_DIAGNOSTICS),
                    )
            except sqlite3.Error:
                raise MarkerLedgerError("LEDGER_WRITE_FAILED") from None
        result = {
            "ok": True,
            "operation_id": operation_id,
            "transport_state": record.transport_state,
        }
        if correlation is not None and record.request_message_id is not None:
            result.update(
                {
                    "request_message_id": record.request_message_id,
                    "request_id": record.request_id,
                    "root_id": record.root_id,
                }
            )
        return result

    if action == "confirm":
        return {"ok": True, **_confirm_existing_record(payload, ledger_path)}

    if action not in {"begin", "state"}:
        raise ValueError("invalid_request")
    ledger = OperationLedger(ledger_path)

    if action == "begin":
        if set(payload) != {
            "action",
            "operation_id",
            "account_identity",
            "team_identity",
            "target_identity",
            "prompt",
        }:
            raise ValueError("invalid_request")
        if payload["operation_id"] != OPERATION_ID or payload["prompt"] != PROMPT:
            raise OperationConflictError("Marker operation input is fixed.")
        record, created = ledger.begin_send(
            operation_id=payload["operation_id"],
            account_identity=payload["account_identity"],
            team_identity=payload["team_identity"],
            target_identity=payload["target_identity"],
            prompt=payload["prompt"],
        )
        # UUID parsing also rejects a malformed or unexpected ledger value.
        uuid.UUID(record.client_nonce)
        return {
            "ok": True,
            "created": created,
            "client_nonce": record.client_nonce,
            "transport_state": record.transport_state,
        }

    if action == "state":
        if set(payload) != {"action", "operation_id", "state"}:
            raise ValueError("invalid_request")
        if payload["operation_id"] != OPERATION_ID or payload["state"] not in _ALLOWED_STATES:
            raise ValueError("invalid_request")
        record = ledger.set_transport_state(payload["operation_id"], payload["state"])
        return {"ok": True, "transport_state": record.transport_state}

    raise ValueError("invalid_request")


def _safe_error(error: BaseException) -> dict[str, Any]:
    if isinstance(error, MarkerLedgerError):
        return {"ok": False, "stage": "ledger", "code": error.code}
    if isinstance(error, AccountMismatchError):
        return {"ok": False, "stage": "ledger", "code": "ACCOUNT_MISMATCH"}
    if isinstance(error, TeamMismatchError):
        return {"ok": False, "stage": "ledger", "code": "TEAM_MISMATCH"}
    if isinstance(error, OperationAnchorError):
        return {"ok": False, "stage": "ledger", "code": str(error)}
    if isinstance(error, OperationConflictError):
        return {"ok": False, "stage": "ledger", "code": "OPERATION_CONFLICT"}
    if isinstance(error, FileNotFoundError):
        return {"ok": False, "stage": "ledger", "code": "LEDGER_UNAVAILABLE"}
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return {"ok": False, "stage": "ledger", "code": "INVALID_REQUEST"}
    return {"ok": False, "stage": "ledger", "code": "LEDGER_FAILURE"}


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    ledger_path: str | Path | None = None,
) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--ledger-adapter"]:
        selected_ledger_path = ledger_path or default_ledger_path()
    elif (
        len(arguments) == 3
        and arguments[:2] == ["--ledger-adapter", "--ledger-path"]
        and Path(arguments[2]).is_absolute()
    ):
        selected_ledger_path = ledger_path or Path(arguments[2])
    else:
        stdout.write(json.dumps({"ok": False, "stage": "arguments", "code": "LEDGER_ADAPTER_REQUIRED"}) + "\n")
        return 2

    try:
        raw = stdin.buffer.read(_MAX_INPUT_BYTES + 1) if hasattr(stdin, "buffer") else stdin.read(_MAX_INPUT_BYTES + 1)
        if isinstance(raw, bytes):
            if len(raw) > _MAX_INPUT_BYTES:
                raise ValueError("invalid_request")
            raw = raw.decode("utf-8")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise ValueError("invalid_request")
        payload = json.loads(raw)
        result = dispatch(payload, selected_ledger_path)
        stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        return 0
    except Exception as error:
        stdout.write(json.dumps(_safe_error(error), separators=(",", ":")) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
