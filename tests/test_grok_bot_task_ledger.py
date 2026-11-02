from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from home_mcp_gateway._grok_bot_marker_ledger import (
    MarkerLedgerError,
    OPERATION_ID as MARKER_OPERATION_ID,
    PROMPT as MARKER_PROMPT,
    dispatch,
    main,
)
from home_mcp_gateway._grok_bot_recovery import OperationConflictError, OperationLedger


TASK_ID = "GROK-TASK-SYNTHETIC-001"
ACCOUNT = "synthetic-approved-account"
TEAM = "2468"
TARGET = "synthetic-approved-bot-id"
PROMPT = "Review this synthetic fixture for a missing null check."
NONCE = "6b6356d7-79de-43ac-a31c-373fd7ec190a"
REQUEST_MESSAGE_ID = "synthetic-request-message-id"
REQUEST_ID = "synthetic-request-id"
ROOT_ID = "synthetic-root-id"


def seed_anchor(path: Path, *, state: str = "accepted", account: str = ACCOUNT,
                team: str = TEAM, target: str = TARGET):
    dispatch({
        "action": "begin",
        "operation_id": MARKER_OPERATION_ID,
        "account_identity": account,
        "team_identity": team,
        "target_identity": target,
        "prompt": MARKER_PROMPT,
    }, path)
    if state != "sending":
        dispatch({"action": "state", "operation_id": MARKER_OPERATION_ID, "state": state}, path)


def task_begin(path: Path, **overrides):
    return dispatch({
        "action": "task_begin",
        "operation_id": TASK_ID,
        "account_identity": ACCOUNT,
        "team_identity": TEAM,
        "target_identity": TARGET,
        "prompt": PROMPT,
        **overrides,
    }, path)


def task_state(path: Path, state: str = "accepted", **overrides):
    payload = {
        "action": "task_state",
        "operation_id": TASK_ID,
        "state": state,
        **overrides,
    }
    return dispatch(payload, path)


def test_task_begin_binds_accepted_marker_and_stores_only_hash_and_nonce(tmp_path):
    path = tmp_path / "synthetic-ledger.sqlite3"
    seed_anchor(path)

    anchor_before = OperationLedger(path).get(MARKER_OPERATION_ID)
    anchor = dispatch({"action": "anchor", "operation_id": MARKER_OPERATION_ID}, path)
    admission = task_begin(path)
    duplicate = task_begin(path)

    assert anchor["transport_state"] == "accepted"
    assert OperationLedger(path).get(MARKER_OPERATION_ID) == anchor_before
    assert admission["ok"] is True
    assert admission["created"] is True
    assert uuid.UUID(admission["client_nonce"]).version == 4
    assert admission["transport_state"] == "sending"
    assert duplicate["created"] is False
    assert duplicate["client_nonce"] == admission["client_nonce"]
    assert duplicate["transport_state"] == "sending"
    record = OperationLedger(path).get(TASK_ID)
    assert record is not None
    assert record.account_identity == ACCOUNT
    assert record.team_identity == TEAM
    assert record.target_identity == TARGET
    assert record.prompt_sha256 == hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
    database = path.read_bytes()
    assert PROMPT.encode("utf-8") not in database


def test_task_admission_conflicts_on_prompt_account_team_or_target(tmp_path):
    path = tmp_path / "synthetic-conflicts.sqlite3"
    seed_anchor(path)
    task_begin(path)

    with pytest.raises(OperationConflictError):
        task_begin(path, prompt="different fixture")
    for key, value in (
        ("account_identity", "different-account"),
        ("team_identity", "different-team"),
        ("target_identity", "different-target"),
    ):
        with pytest.raises(MarkerLedgerError) as error:
            task_begin(path, **{key: value})
        assert error.value.code == "MARKER_ANCHOR_MISMATCH"
    assert OperationLedger(path).get(TASK_ID).client_nonce


@pytest.mark.parametrize("state", ["sending", "unknown", "rejected"])
def test_task_admission_requires_the_accepted_marker_anchor(tmp_path, state):
    path = tmp_path / f"synthetic-anchor-{state}.sqlite3"
    seed_anchor(path, state=state)

    with pytest.raises(MarkerLedgerError) as error:
        task_begin(path)

    assert error.value.code == "MARKER_ANCHOR_NOT_ACCEPTED"
    assert OperationLedger(path).get(TASK_ID) is None


def test_task_admission_rejects_context_that_drifted_from_anchor(tmp_path):
    path = tmp_path / "synthetic-anchor-drift.sqlite3"
    seed_anchor(path)

    with pytest.raises(MarkerLedgerError) as error:
        task_begin(path, target_identity="different-bot-id")

    assert error.value.code == "MARKER_ANCHOR_MISMATCH"
    assert OperationLedger(path).get(TASK_ID) is None


def test_task_read_of_missing_row_is_read_only_and_does_not_create_database(tmp_path):
    path = tmp_path / "must-not-exist.sqlite3"

    with pytest.raises(MarkerLedgerError) as error:
        dispatch({"action": "task_read", "operation_id": TASK_ID}, path)

    assert error.value.code == "OPERATION_NOT_FOUND"
    assert not path.exists()


def test_task_read_accepts_pre_correlation_schema_and_migration_preserves_row(tmp_path):
    path = tmp_path / "synthetic-legacy-task.sqlite3"
    prompt_hash = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                account_identity TEXT NOT NULL,
                team_identity TEXT NOT NULL,
                client_nonce TEXT NOT NULL UNIQUE,
                target_identity TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL,
                transport_state TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO operations (
                operation_id, account_identity, team_identity, client_nonce,
                target_identity, prompt_sha256, transport_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (TASK_ID, ACCOUNT, TEAM, NONCE, TARGET, prompt_hash, "sending"),
        )

    legacy_read = dispatch({"action": "task_read", "operation_id": TASK_ID}, path)
    assert legacy_read["operation_id"] == TASK_ID
    assert legacy_read["diagnostics"] == []
    assert "request_message_id" not in legacy_read
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_diagnostics'"
        ).fetchone() is None
    migrated = OperationLedger(path).get(TASK_ID)
    assert migrated is not None
    assert migrated.request_message_id is None
    assert migrated.request_id is None
    assert migrated.root_id is None


def test_concurrent_task_admission_has_one_sender_and_one_durable_nonce(tmp_path):
    path = tmp_path / "synthetic-concurrent.sqlite3"
    seed_anchor(path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: task_begin(path), range(8)))

    assert sum(result["created"] for result in results) == 1
    assert len({result["client_nonce"] for result in results}) == 1
    assert {result["transport_state"] for result in results} == {"sending"}


def test_unknown_timeout_and_restart_never_reopen_send_admission(tmp_path):
    path = tmp_path / "synthetic-timeout.sqlite3"
    seed_anchor(path)
    first = task_begin(path)
    dispatch({"action": "task_state", "operation_id": TASK_ID, "state": "unknown"}, path)

    restarted = task_begin(path)

    assert restarted["created"] is False
    assert restarted["client_nonce"] == first["client_nonce"]
    assert restarted["transport_state"] == "unknown"
    accepted = dispatch({"action": "task_state", "operation_id": TASK_ID, "state": "accepted"}, path)
    late_timeout = dispatch({"action": "task_state", "operation_id": TASK_ID, "state": "unknown"}, path)
    assert accepted["transport_state"] == "accepted"
    assert late_timeout["transport_state"] == "accepted"
    assert task_begin(path)["created"] is False


def _correlation_payload(path: Path, *, state: str = "accepted", **overrides):
    record = OperationLedger(path).get(TASK_ID)
    assert record is not None
    return {
        "action": "task_state",
        "operation_id": TASK_ID,
        "state": state,
        "account_identity": record.account_identity,
        "team_identity": record.team_identity,
        "target_identity": record.target_identity,
        "client_nonce": record.client_nonce,
        "prompt_sha256": record.prompt_sha256,
        "request_message_id": REQUEST_MESSAGE_ID,
        "request_id": REQUEST_ID,
        "root_id": ROOT_ID,
        **overrides,
    }


def _diagnostic(**overrides):
    return {
        "phase": "send_receipt",
        "reason": "RECEIPT_MESSAGE_ID_MISSING",
        "response_type": "object",
        "present_keys": ["messageId"],
        "unknown_key_count": 0,
        "http_status": 200,
        "observed_at": "2026-10-28T01:23:45.678Z",
        **overrides,
    }


def test_task_state_persists_and_task_read_returns_validated_correlation(tmp_path):
    path = tmp_path / "synthetic-correlation.sqlite3"
    seed_anchor(path)
    task_begin(path)

    result = dispatch(_correlation_payload(path, diagnostic=_diagnostic()), path)
    read_back = dispatch({"action": "task_read", "operation_id": TASK_ID}, path)
    record = OperationLedger(path).get(TASK_ID)

    assert result["transport_state"] == "accepted"
    assert result["request_message_id"] == REQUEST_MESSAGE_ID
    assert result["request_id"] == REQUEST_ID
    assert result["root_id"] == ROOT_ID
    assert read_back["request_message_id"] == REQUEST_MESSAGE_ID
    assert read_back["request_id"] == REQUEST_ID
    assert read_back["root_id"] == ROOT_ID
    assert read_back["diagnostics"] == [_diagnostic()]
    assert record is not None
    assert record.request_message_id == REQUEST_MESSAGE_ID
    assert record.request_id == REQUEST_ID
    assert record.root_id == ROOT_ID

    late_timeout = task_state(path, state="unknown")
    assert late_timeout["transport_state"] == "accepted"
    assert dispatch({"action": "task_read", "operation_id": TASK_ID}, path)[
        "request_id"
    ] == REQUEST_ID


def test_task_state_persists_validated_diagnostic_and_task_read_returns_it(tmp_path):
    path = tmp_path / "synthetic-diagnostic.sqlite3"
    seed_anchor(path)
    task_begin(path)

    state_result = task_state(path, diagnostic=_diagnostic(
        phase="request_lookup",
        reason="REQUEST_NONCE_NOT_FOUND",
        tail_entries=2,
        nonce_matches=0,
    ))
    read_back = dispatch({"action": "task_read", "operation_id": TASK_ID}, path)

    assert state_result["transport_state"] == "accepted"
    assert read_back["diagnostics"] == [_diagnostic(
        phase="request_lookup",
        reason="REQUEST_NONCE_NOT_FOUND",
        tail_entries=2,
        nonce_matches=0,
    )]
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT diagnostic_json FROM operation_diagnostics WHERE operation_id=?",
            (TASK_ID,),
        ).fetchone()[0]
    assert "prompt" not in stored
    assert "token" not in stored


@pytest.mark.parametrize("diagnostic", [
    _diagnostic(unknown_secret_key="synthetic-secret-token"),
    _diagnostic(reason="NOT_A_FIXED_ENUM"),
    _diagnostic(observed_at="x" * 5000),
])
def test_task_state_rejects_invalid_diagnostics(tmp_path, diagnostic):
    path = tmp_path / f"synthetic-invalid-diagnostic-{len(str(diagnostic))}.sqlite3"
    seed_anchor(path)
    task_begin(path)

    with pytest.raises(MarkerLedgerError) as error:
        task_state(path, diagnostic=diagnostic)

    assert error.value.code == "OPERATION_RECORD_INVALID"
    read_back = dispatch({"action": "task_read", "operation_id": TASK_ID}, path)
    assert read_back["diagnostics"] == []


def test_task_state_rejects_partial_correlation_payload(tmp_path):
    path = tmp_path / "synthetic-partial-correlation.sqlite3"
    seed_anchor(path)
    task_begin(path)
    payload = _correlation_payload(path)

    for key in ("request_message_id", "request_id", "root_id"):
        partial = {name: value for name, value in payload.items() if name != key}
        with pytest.raises(MarkerLedgerError) as error:
            dispatch(partial, path)
        assert error.value.code == "OPERATION_RECORD_INVALID"

    assert dispatch({"action": "task_read", "operation_id": TASK_ID}, path).get(
        "request_id"
    ) is None


def test_task_state_rejects_binding_mismatch_before_attaching_ids(tmp_path):
    path = tmp_path / "synthetic-correlation-binding.sqlite3"
    seed_anchor(path)
    task_begin(path)
    payload = _correlation_payload(path)

    for key, value in (
        ("account_identity", "different-account"),
        ("team_identity", "different-team"),
        ("target_identity", "different-target"),
        ("client_nonce", "3b3c4a4a-9948-4aac-bd34-7bff9e2fef04"),
        ("prompt_sha256", "f" * 64),
    ):
        with pytest.raises(MarkerLedgerError) as error:
            dispatch({**payload, key: value}, path)
        assert error.value.code == "OPERATION_BINDING_MISMATCH"

    read_back = dispatch({"action": "task_read", "operation_id": TASK_ID}, path)
    assert "request_id" not in read_back


def test_task_state_rejects_conflicting_correlation_ids(tmp_path):
    path = tmp_path / "synthetic-correlation-conflict.sqlite3"
    seed_anchor(path)
    task_begin(path)
    payload = _correlation_payload(path)
    dispatch(payload, path)

    with pytest.raises(OperationConflictError):
        dispatch({**payload, "request_id": "different-request-id"}, path)

    read_back = dispatch({"action": "task_read", "operation_id": TASK_ID}, path)
    assert read_back["request_message_id"] == REQUEST_MESSAGE_ID
    assert read_back["request_id"] == REQUEST_ID
    assert read_back["root_id"] == ROOT_ID


def test_task_actions_cannot_repurpose_or_mutate_the_fixed_marker_operation(tmp_path):
    path = tmp_path / "synthetic-marker-preserved.sqlite3"
    seed_anchor(path)
    before = OperationLedger(path).get(MARKER_OPERATION_ID)

    with pytest.raises(ValueError):
        dispatch({
            "action": "task_begin",
            "operation_id": MARKER_OPERATION_ID,
            "account_identity": ACCOUNT,
            "team_identity": TEAM,
            "target_identity": TARGET,
            "prompt": PROMPT,
        }, path)
    with pytest.raises(ValueError):
        dispatch({"action": "task_state", "operation_id": MARKER_OPERATION_ID, "state": "unknown"}, path)
    with pytest.raises(ValueError):
        dispatch({"action": "task_read", "operation_id": MARKER_OPERATION_ID}, path)

    assert OperationLedger(path).get(MARKER_OPERATION_ID) == before


def test_private_stdin_bridge_accepts_the_bounded_ordinary_prompt_limit(tmp_path):
    path = tmp_path / "synthetic-bridge-limit.sqlite3"
    seed_anchor(path)
    payload = {
        "action": "task_begin",
        "operation_id": TASK_ID,
        "account_identity": ACCOUNT,
        "team_identity": TEAM,
        "target_identity": TARGET,
        "prompt": "x" * (32 * 1024),
    }
    stdin = io.BytesIO(json.dumps(payload).encode("utf-8"))
    stdout = io.StringIO()

    code = main(["--ledger-adapter"], stdin=stdin, stdout=stdout, ledger_path=path)

    assert code == 0
    result = json.loads(stdout.getvalue())
    assert result["created"] is True
    assert result["prompt_sha256"] == hashlib.sha256(payload["prompt"].encode("utf-8")).hexdigest()
    assert payload["prompt"].encode("utf-8") not in path.read_bytes()
