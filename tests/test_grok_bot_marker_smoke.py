from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
from pathlib import Path
import sqlite3
import uuid

import pytest

from home_mcp_gateway._grok_bot_marker_ledger import (
    MarkerLedgerError,
    OPERATION_ID,
    PROMPT,
    dispatch,
    main,
)
from home_mcp_gateway._grok_bot_recovery import (
    AccountMismatchError,
    GatewayRoute,
    GrokBotRecoveryHarness,
    OperationConflictError,
    OperationLedger,
    TeamMismatchError,
    ThreadObservation,
)


TEAM = "1234"
ACCOUNT = "synthetic-account"
TARGET = "synthetic-opaque-bot-id"


def begin(ledger_path: Path, **overrides):
    payload = {
        "action": "begin",
        "operation_id": OPERATION_ID,
        "account_identity": ACCOUNT,
        "team_identity": TEAM,
        "target_identity": TARGET,
        "prompt": PROMPT,
        **overrides,
    }
    return dispatch(payload, ledger_path)


def test_atomic_begin_persists_uuid_and_only_nonsecret_operation_metadata(tmp_path):
    path = tmp_path / "synthetic.sqlite3"
    result = begin(path)

    assert result["ok"] is True
    assert result["created"] is True
    assert uuid.UUID(result["client_nonce"]).version == 4
    assert result["transport_state"] == "sending"

    record = OperationLedger(path).get(OPERATION_ID)
    assert record is not None
    assert record.operation_id == OPERATION_ID
    assert record.account_identity == ACCOUNT
    assert record.team_identity == TEAM
    assert record.target_identity == TARGET
    assert record.client_nonce == result["client_nonce"]
    assert record.prompt_sha256 == hashlib.sha256(PROMPT.encode()).hexdigest()
    assert record.transport_state == "sending"
    database = path.read_bytes()
    assert PROMPT.encode() not in database


def test_concurrent_admission_has_one_sender_and_one_stable_nonce(tmp_path):
    path = tmp_path / "concurrent.sqlite3"

    def admit(_):
        return begin(path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(admit, range(8)))

    assert sum(result["created"] for result in results) == 1
    assert len({result["client_nonce"] for result in results}) == 1
    assert {result["transport_state"] for result in results} == {"sending"}


def test_restarts_conflicts_and_team_drift_never_reopen_send_admission(tmp_path):
    path = tmp_path / "restart.sqlite3"
    first = begin(path)
    reopened = begin(path)
    assert reopened["created"] is False
    assert reopened["client_nonce"] == first["client_nonce"]
    assert reopened["transport_state"] == "sending"

    with pytest.raises(AccountMismatchError):
        begin(path, account_identity="different-account")
    with pytest.raises(TeamMismatchError):
        begin(path, team_identity="5678")
    with pytest.raises(OperationConflictError):
        begin(path, target_identity="different-target")
    with pytest.raises(OperationConflictError):
        begin(path, prompt="different prompt")

    assert begin(path)["created"] is False


def test_ledger_adapter_rejects_nonfixed_operation_and_updates_only_state(tmp_path):
    path = tmp_path / "adapter.sqlite3"
    first = begin(path)
    updated = dispatch(
        {"action": "state", "operation_id": OPERATION_ID, "state": "accepted"},
        path,
    )
    assert updated == {"ok": True, "transport_state": "accepted"}
    assert OperationLedger(path).get(OPERATION_ID).transport_state == "accepted"
    with pytest.raises(OperationConflictError):
        begin(path, operation_id="GROK-TRIAL-20261023-1217")
    assert first["client_nonce"] == OperationLedger(path).get(OPERATION_ID).client_nonce


def test_reconciliation_read_of_missing_ledger_does_not_create_it(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(MarkerLedgerError) as error:
        dispatch({"action": "read", "operation_id": OPERATION_ID}, path)
    assert error.value.code == "OPERATION_NOT_FOUND"
    assert not path.exists()


def test_reconciliation_read_of_empty_ledger_fails_without_initializing_or_inserting(tmp_path):
    path = tmp_path / "empty.sqlite3"
    OperationLedger(path)
    before = path.read_bytes()
    with pytest.raises(MarkerLedgerError) as error:
        dispatch({"action": "read", "operation_id": OPERATION_ID}, path)
    assert error.value.code == "OPERATION_NOT_FOUND"
    assert path.read_bytes() == before
    assert OperationLedger(path).get(OPERATION_ID) is None


def test_reconciliation_read_returns_fixed_binding_without_mutating_live_style_state(tmp_path):
    path = tmp_path / "read-only.sqlite3"
    admitted = begin(path)
    dispatch({"action": "state", "operation_id": OPERATION_ID, "state": "unknown"}, path)
    record_before = OperationLedger(path).get(OPERATION_ID)

    result = dispatch({"action": "read", "operation_id": OPERATION_ID}, path)

    assert result == {
        "ok": True,
        "operation_id": OPERATION_ID,
        "account_identity": ACCOUNT,
        "team_identity": TEAM,
        "client_nonce": admitted["client_nonce"],
        "target_identity": TARGET,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "transport_state": "unknown",
    }
    assert OperationLedger(path).get(OPERATION_ID) == record_before


def test_reconciliation_read_rejects_malformed_existing_binding(tmp_path):
    path = tmp_path / "malformed.sqlite3"
    begin(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE operations SET prompt_sha256=? WHERE operation_id=?",
            ("f" * 64, OPERATION_ID),
        )
    with pytest.raises(MarkerLedgerError) as error:
        dispatch({"action": "read", "operation_id": OPERATION_ID}, path)
    assert error.value.code == "OPERATION_RECORD_INVALID"


def test_reconcile_confirmation_updates_only_existing_bound_row(tmp_path):
    path = tmp_path / "confirm.sqlite3"
    admitted = begin(path)
    evidence = {
        "action": "confirm",
        "operation_id": OPERATION_ID,
        "account_identity": ACCOUNT,
        "team_identity": TEAM,
        "target_identity": TARGET,
        "client_nonce": admitted["client_nonce"],
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "request_message_id": "synthetic-request-message-id",
        "request_id": "synthetic-request-run-id",
        "root_id": "synthetic-root-id",
    }
    result = dispatch(evidence, path)
    assert result == {"ok": True, "operation_id": OPERATION_ID, "transport_state": "accepted"}
    assert OperationLedger(path).get(OPERATION_ID).transport_state == "accepted"

    with pytest.raises(MarkerLedgerError) as error:
        dispatch({**evidence, "target_identity": "different-target"}, path)
    assert error.value.code == "OPERATION_BINDING_MISMATCH"
    assert OperationLedger(path).get(OPERATION_ID).target_identity == TARGET


def test_reconcile_confirmation_never_creates_a_missing_ledger(tmp_path):
    path = tmp_path / "missing-confirm.sqlite3"
    evidence = {
        "action": "confirm",
        "operation_id": OPERATION_ID,
        "account_identity": ACCOUNT,
        "team_identity": TEAM,
        "target_identity": TARGET,
        "client_nonce": "e5dd8c58-bd07-4c46-9f22-e5a19a8a0f17",
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "request_message_id": "synthetic-request-message-id",
        "request_id": None,
        "root_id": "synthetic-root-id",
    }
    with pytest.raises(MarkerLedgerError) as error:
        dispatch(evidence, path)
    assert error.value.code == "OPERATION_NOT_FOUND"
    assert not path.exists()


def test_confirmed_acceptance_cannot_be_downgraded_by_a_late_timeout(tmp_path):
    path = tmp_path / "monotonic.sqlite3"
    begin(path)
    ledger = OperationLedger(path)
    ledger.set_transport_state(OPERATION_ID, "accepted")
    after_timeout = ledger.set_transport_state(OPERATION_ID, "unknown")
    assert after_timeout.transport_state == "accepted"


def test_adapter_import_and_default_gate_do_not_create_the_live_ledger(tmp_path):
    path = tmp_path / "must-not-exist.sqlite3"
    stdout = io.StringIO()
    code = main([], stdin=io.StringIO("{}"), stdout=stdout, ledger_path=path)
    assert code == 2
    assert not path.exists()
    assert "LEDGER_ADAPTER_REQUIRED" in stdout.getvalue()


def test_legacy_ledger_migrates_team_pin_as_unbound(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                account_identity TEXT NOT NULL,
                client_nonce TEXT NOT NULL UNIQUE,
                target_identity TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL,
                transport_state TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-op", ACCOUNT, "legacy-nonce", TARGET, "a" * 64, "sending"),
        )
    record = OperationLedger(path).get("legacy-op")
    assert record is not None
    assert record.team_identity == "unbound"


def test_nonce_matched_id_upgrades_uncertain_delivery_without_remote_terminal_claim(tmp_path):
    path = tmp_path / "correlated.sqlite3"
    ledger = OperationLedger(path)
    route = GatewayRoute(
        account_identity=ACCOUNT,
        gateway_url="https://synthetic.cursorvm.com",
        gateway_token="synthetic-route-token",
    )
    send_calls = []

    def send_prompt(_route, target, prompt, extra):
        send_calls.append((target, prompt, extra["clientNonce"]))
        return {"accepted": True}

    def read_thread(_route, target, nonce):
        record = ledger.get(OPERATION_ID)
        assert record is not None
        return ThreadObservation(
            correlation_nonce=nonce,
            request_id_observed=True,
            root_id_observed=True,
            reply="GROK-HARNESS-20261024-1652-OK",
        )

    harness = GrokBotRecoveryHarness(
        ledger=ledger,
        send_prompt=send_prompt,
        read_thread=read_thread,
    )
    outcome = harness.submit(
        route=route,
        operation_id=OPERATION_ID,
        client_nonce="fixture-nonce",
        target_identity=TARGET,
        prompt=PROMPT,
        team_identity=TEAM,
    )
    assert len(send_calls) == 1
    assert outcome.transport_status == "accepted"
    assert outcome.correlation_confirmed is True
    assert outcome.reply == "GROK-HARNESS-20261024-1652-OK"
    assert outcome.remote_terminal == "unsupported"
    assert ledger.get(OPERATION_ID).transport_state == "accepted"
