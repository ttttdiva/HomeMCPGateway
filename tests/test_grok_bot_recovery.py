from __future__ import annotations

import hashlib

import pytest

from home_mcp_gateway._grok_bot_recovery import (
    ENSURE_SANDBOX_ENDPOINT,
    AccountMismatchError,
    EndpointPolicyError,
    EnsureSandboxRequest,
    EnsureSandboxResponse,
    ExistingAccount,
    GatewayRoute,
    GrokBotRecoveryHarness,
    OperationConflictError,
    OperationLedger,
    ThreadObservation,
    recover_existing_account_route,
    validate_ensure_sandbox_endpoint,
)


ACCOUNT = "synthetic-account-01"
TARGET = "synthetic-bot-id-01"
PROMPT = "synthetic prompt that must not be persisted"
NONCE = "synthetic-client-nonce-01"
OPERATION_ID = "synthetic-operation-01"


def ensure_response(
    *,
    account_identity: str = ACCOUNT,
    response_url: str = ENSURE_SANDBOX_ENDPOINT,
    gateway_url: str = "https://bot-123.us12.cursorvm.com",
    gateway_token: str = "synthetic-route-token",
    status_code: int = 200,
    redirect_history: tuple[str, ...] = (),
) -> EnsureSandboxResponse:
    return EnsureSandboxResponse(
        account_identity=account_identity,
        status_code=status_code,
        response_url=response_url,
        gateway_url=gateway_url,
        gateway_token=gateway_token,
        redirect_history=redirect_history,
    )


def gateway_route(token: str = "synthetic-route-token") -> GatewayRoute:
    return GatewayRoute(
        account_identity=ACCOUNT,
        gateway_url="https://bot-123.us12.cursorvm.com",
        gateway_token=token,
    )


def recover_route(
    *,
    state: str,
    ensure_sandbox,
    existing_route: GatewayRoute | None = None,
) -> GatewayRoute:
    return recover_existing_account_route(
        account=ExistingAccount(identity=ACCOUNT, active=True),
        expected_account_identity=ACCOUNT,
        descriptor_state=state,
        existing_route=existing_route,
        ensure_sandbox=ensure_sandbox,
    )


def test_absent_descriptor_recovers_with_the_exact_endpoint_and_no_redirects():
    requests: list[EnsureSandboxRequest] = []

    def ensure(request: EnsureSandboxRequest) -> EnsureSandboxResponse:
        requests.append(request)
        return ensure_response()

    route = recover_route(state="absent", ensure_sandbox=ensure)

    assert len(requests) == 1
    assert requests[0].endpoint == ENSURE_SANDBOX_ENDPOINT
    assert requests[0].redirect_policy == "error"
    assert route.gateway_url == "https://bot-123.us12.cursorvm.com"
    assert "synthetic-route-token" not in repr(route)


def test_asleep_descriptor_uses_fresh_route_from_existing_account():
    calls = 0
    stale = gateway_route("synthetic-stale-route-token")

    def ensure(request: EnsureSandboxRequest) -> EnsureSandboxResponse:
        nonlocal calls
        calls += 1
        assert request.account_identity == ACCOUNT
        return ensure_response(gateway_token="synthetic-recovered-route-token")

    route = recover_route(
        state="asleep", ensure_sandbox=ensure, existing_route=stale
    )

    assert calls == 1
    assert route.gateway_token == "synthetic-recovered-route-token"
    assert route.gateway_token != stale.gateway_token


def test_account_mismatch_fails_before_ensure_sandbox():
    calls = 0

    def ensure(request: EnsureSandboxRequest) -> EnsureSandboxResponse:
        nonlocal calls
        calls += 1
        return ensure_response()

    with pytest.raises(AccountMismatchError):
        recover_existing_account_route(
            account=ExistingAccount(identity="synthetic-account-other", active=True),
            expected_account_identity=ACCOUNT,
            descriptor_state="absent",
            existing_route=None,
            ensure_sandbox=ensure,
        )

    assert calls == 0


def test_inactive_account_cannot_recover_a_route():
    with pytest.raises(AccountMismatchError):
        recover_existing_account_route(
            account=ExistingAccount(identity=ACCOUNT, active=False),
            expected_account_identity=ACCOUNT,
            descriptor_state="absent",
            existing_route=None,
            ensure_sandbox=lambda request: ensure_response(),
        )


def test_ensure_sandbox_account_drift_fails_closed():
    with pytest.raises(AccountMismatchError):
        recover_route(
            state="absent",
            ensure_sandbox=lambda request: ensure_response(
                account_identity="synthetic-account-switched"
            ),
        )


def test_bad_endpoint_redirect_and_unapproved_gateway_host_fail_closed():
    with pytest.raises(EndpointPolicyError):
        validate_ensure_sandbox_endpoint(
            "https://api2.cursor.sh.attacker.example/aiserver.v1.GrokBotService/EnsureSandBox"
        )

    with pytest.raises(EndpointPolicyError):
        recover_route(
            state="absent",
            ensure_sandbox=lambda request: ensure_response(
                response_url="https://redirect.example/EnsureSandBox",
                redirect_history=("https://redirect.example/EnsureSandBox",),
            ),
        )

    with pytest.raises(EndpointPolicyError):
        recover_route(
            state="absent",
            ensure_sandbox=lambda request: ensure_response(
                # A followed chain can return to the allowed URL; history still fails.
                response_url=ENSURE_SANDBOX_ENDPOINT,
                redirect_history=("https://redirect.example/EnsureSandBox",),
            ),
        )

    with pytest.raises(EndpointPolicyError):
        recover_route(
            state="absent",
            ensure_sandbox=lambda request: ensure_response(
                redirect_history=None  # type: ignore[arg-type]
            ),
        )

    with pytest.raises(EndpointPolicyError):
        recover_route(
            state="absent",
            ensure_sandbox=lambda request: ensure_response(
                gateway_url="https://bot.attacker.example"
            ),
        )


def test_client_nonce_and_operation_metadata_are_committed_before_send(tmp_path):
    ledger_path = tmp_path / "grok-bot-operations.sqlite3"
    ledger = OperationLedger(ledger_path)
    send_calls: list[tuple[str, str, str]] = []

    def send_prompt(route, target_identity, prompt, extra):
        record = ledger.get(OPERATION_ID)
        assert record is not None
        assert record.transport_state == "sending"
        assert record.client_nonce == NONCE
        assert record.target_identity == TARGET
        assert record.prompt_sha256 == hashlib.sha256(prompt.encode()).hexdigest()
        assert extra == {"clientNonce": NONCE}
        send_calls.append((target_identity, prompt, extra["clientNonce"]))
        return {"delivery": "accepted", "messageId": "synthetic-message-id"}

    harness = GrokBotRecoveryHarness(
        ledger=ledger,
        send_prompt=send_prompt,
        read_thread=lambda route, target, nonce: ThreadObservation(),
    )
    outcome = harness.submit(
        route=gateway_route(),
        operation_id=OPERATION_ID,
        client_nonce=NONCE,
        target_identity=TARGET,
        prompt=PROMPT,
    )

    assert send_calls == [(TARGET, PROMPT, NONCE)]
    assert outcome.transport_status == "accepted"
    assert outcome.reply is None
    assert outcome.remote_terminal == "unsupported"
    ledger_bytes = ledger_path.read_bytes()
    assert PROMPT.encode() not in ledger_bytes
    assert b"synthetic-route-token" not in ledger_bytes


def test_duplicate_operation_reads_only_and_changed_input_is_rejected(tmp_path):
    ledger = OperationLedger(tmp_path / "operations.sqlite3")
    send_count = 0
    thread_reads = 0

    def send_prompt(route, target_identity, prompt, extra):
        nonlocal send_count
        send_count += 1
        return {"delivery": "accepted", "messageId": "synthetic-message-id"}

    def read_thread(route, target_identity, nonce):
        nonlocal thread_reads
        thread_reads += 1
        return ThreadObservation(
            correlation_nonce=nonce,
            reply="synthetic reply",
            terminal_supported=False,
        )

    harness = GrokBotRecoveryHarness(
        ledger=ledger, send_prompt=send_prompt, read_thread=read_thread
    )
    arguments = {
        "route": gateway_route(),
        "operation_id": OPERATION_ID,
        "client_nonce": NONCE,
        "target_identity": TARGET,
        "prompt": PROMPT,
    }

    first = harness.submit(**arguments)
    replay = harness.submit(**arguments)

    assert first.transport_status == "accepted"
    assert replay.transport_status == "accepted"
    assert replay.reply == "synthetic reply"
    assert send_count == 1
    assert thread_reads == 1
    with pytest.raises(OperationConflictError):
        harness.submit(**{**arguments, "prompt": "changed prompt"})
    assert send_count == 1
    assert thread_reads == 1


def test_operation_replay_and_reconcile_are_bound_to_account(tmp_path):
    ledger = OperationLedger(tmp_path / "operations.sqlite3")
    send_count = 0
    thread_reads = 0

    def send_prompt(route, target_identity, prompt, extra):
        nonlocal send_count
        send_count += 1
        return {"delivery": "accepted", "messageId": "synthetic-message-id"}

    def read_thread(route, target_identity, nonce):
        nonlocal thread_reads
        thread_reads += 1
        return ThreadObservation()

    harness = GrokBotRecoveryHarness(
        ledger=ledger, send_prompt=send_prompt, read_thread=read_thread
    )
    arguments = {
        "route": gateway_route(),
        "operation_id": OPERATION_ID,
        "client_nonce": NONCE,
        "target_identity": TARGET,
        "prompt": PROMPT,
    }
    harness.submit(**arguments)
    other_account_route = GatewayRoute(
        account_identity="synthetic-account-other",
        gateway_url="https://other-123.us12.cursorvm.com",
        gateway_token="synthetic-other-route-token",
    )

    with pytest.raises(AccountMismatchError):
        harness.submit(**{**arguments, "route": other_account_route})
    with pytest.raises(AccountMismatchError):
        harness.reconcile(
            route=other_account_route, operation_id=OPERATION_ID
        )

    assert send_count == 1
    assert thread_reads == 0


def test_uncertain_send_reconciles_by_thread_read_and_never_replays_after_restart(
    tmp_path,
):
    ledger = OperationLedger(tmp_path / "operations.sqlite3")
    timeout = TimeoutError("synthetic timeout after possible send")
    timeout.delivery = "unknown"
    send_count = 0
    thread_reads = 0

    def send_prompt(route, target_identity, prompt, extra):
        nonlocal send_count
        send_count += 1
        raise timeout

    def read_thread(route, target_identity, nonce):
        nonlocal thread_reads
        thread_reads += 1
        record = ledger.get(OPERATION_ID)
        assert record is not None
        assert record.transport_state == "unknown"
        return ThreadObservation()

    harness = GrokBotRecoveryHarness(
        ledger=ledger, send_prompt=send_prompt, read_thread=read_thread
    )
    arguments = {
        "route": gateway_route(),
        "operation_id": OPERATION_ID,
        "client_nonce": NONCE,
        "target_identity": TARGET,
        "prompt": PROMPT,
    }

    first = harness.submit(**arguments)
    replay_ledger = OperationLedger(tmp_path / "operations.sqlite3")

    def forbidden_send(*args):
        pytest.fail("a persisted uncertain operation must never be resent")

    replay_harness = GrokBotRecoveryHarness(
        ledger=replay_ledger,
        send_prompt=forbidden_send,
        read_thread=read_thread,
    )
    replay = replay_harness.submit(**arguments)

    assert first.transport_status == "unknown"
    assert first.send_error is timeout
    assert replay.transport_status == "unknown"
    assert send_count == 1
    assert thread_reads == 2


def test_receipt_without_message_id_is_unknown_and_only_reads_thread(tmp_path):
    ledger = OperationLedger(tmp_path / "operations.sqlite3")
    send_count = 0
    thread_reads = 0

    def send_prompt(route, target_identity, prompt, extra):
        nonlocal send_count
        send_count += 1
        return {"delivery": "accepted"}

    def read_thread(route, target_identity, nonce):
        nonlocal thread_reads
        thread_reads += 1
        return ThreadObservation()

    outcome = GrokBotRecoveryHarness(
        ledger=ledger, send_prompt=send_prompt, read_thread=read_thread
    ).submit(
        route=gateway_route(),
        operation_id=OPERATION_ID,
        client_nonce=NONCE,
        target_identity=TARGET,
        prompt=PROMPT,
    )

    assert outcome.transport_status == "unknown"
    assert outcome.reconciled is True
    assert send_count == 1
    assert thread_reads == 1


def test_crash_after_send_boundary_recovers_as_unknown_with_read_only_thread(
    tmp_path,
):
    ledger_path = tmp_path / "operations.sqlite3"
    ledger = OperationLedger(ledger_path)

    class SimulatedCrash(BaseException):
        pass

    def crash_after_possible_send(route, target_identity, prompt, extra):
        raise SimulatedCrash()

    interrupted = GrokBotRecoveryHarness(
        ledger=ledger,
        send_prompt=crash_after_possible_send,
        read_thread=lambda route, target, nonce: ThreadObservation(),
    )
    arguments = {
        "route": gateway_route(),
        "operation_id": OPERATION_ID,
        "client_nonce": NONCE,
        "target_identity": TARGET,
        "prompt": PROMPT,
    }

    with pytest.raises(SimulatedCrash):
        interrupted.submit(**arguments)

    saved = OperationLedger(ledger_path).get(OPERATION_ID)
    assert saved is not None
    assert saved.transport_state == "sending"
    assert saved.client_nonce == NONCE

    def forbidden_send(*args):
        pytest.fail("crash recovery must reconcile by thread read only")

    reads: list[tuple[str, str]] = []

    def read_thread(route, target_identity, nonce):
        reads.append((target_identity, nonce))
        return ThreadObservation()

    recovered = GrokBotRecoveryHarness(
        ledger=OperationLedger(ledger_path),
        send_prompt=forbidden_send,
        read_thread=read_thread,
    ).submit(**arguments)

    assert recovered.transport_status == "unknown"
    assert recovered.reconciled is True
    assert reads == [(TARGET, NONCE)]


def test_uncertain_send_and_thread_errors_are_preserved_without_replay(tmp_path):
    ledger = OperationLedger(tmp_path / "operations.sqlite3")
    send_error = TimeoutError("synthetic send timeout")
    send_error.delivery = "unknown"
    thread_error = OSError("synthetic thread read failure")
    send_count = 0

    def send_prompt(route, target_identity, prompt, extra):
        nonlocal send_count
        send_count += 1
        raise send_error

    def fail_thread_read(*args):
        raise thread_error

    harness = GrokBotRecoveryHarness(
        ledger=ledger,
        send_prompt=send_prompt,
        read_thread=fail_thread_read,
    )
    outcome = harness.submit(
        route=gateway_route(),
        operation_id=OPERATION_ID,
        client_nonce=NONCE,
        target_identity=TARGET,
        prompt=PROMPT,
    )

    assert outcome.transport_status == "unknown"
    assert outcome.send_error is send_error
    assert outcome.reconciliation_error is thread_error
    assert outcome.reconciled is False
    assert send_count == 1


def test_reply_and_remote_terminal_are_independent_observations(tmp_path):
    ledger = OperationLedger(tmp_path / "operations.sqlite3")
    harness = GrokBotRecoveryHarness(
        ledger=ledger,
        send_prompt=lambda *args: {
            "delivery": "accepted",
            "messageId": "synthetic-message-id",
        },
        read_thread=lambda route, target, nonce: ThreadObservation(
            correlation_nonce=nonce,
            reply="synthetic actual reply",
            remote_terminal="completed",
            terminal_supported=True,
        ),
    )
    sent = harness.submit(
        route=gateway_route(),
        operation_id=OPERATION_ID,
        client_nonce=NONCE,
        target_identity=TARGET,
        prompt=PROMPT,
    )
    observed = harness.reconcile(
        route=gateway_route(), operation_id=OPERATION_ID
    )

    assert sent.transport_status == "accepted"
    assert sent.reply is None
    assert observed.transport_status == "accepted"
    assert observed.reply == "synthetic actual reply"
    assert observed.remote_terminal == "completed"
    assert observed.terminal_supported is True


def test_terminal_without_reply_does_not_claim_a_reply(tmp_path):
    ledger = OperationLedger(tmp_path / "operations.sqlite3")
    harness = GrokBotRecoveryHarness(
        ledger=ledger,
        send_prompt=lambda *args: {
            "delivery": "accepted",
            "messageId": "synthetic-message-id",
        },
        read_thread=lambda route, target, nonce: ThreadObservation(
            correlation_nonce=nonce,
            reply=None,
            remote_terminal="cancelled",
            terminal_supported=True,
        ),
    )
    harness.submit(
        route=gateway_route(),
        operation_id=OPERATION_ID,
        client_nonce=NONCE,
        target_identity=TARGET,
        prompt=PROMPT,
    )
    observed = harness.reconcile(
        route=gateway_route(), operation_id=OPERATION_ID
    )

    assert observed.reply is None
    assert observed.remote_terminal == "cancelled"
    assert observed.terminal_supported is True


def test_uncorrelated_thread_reply_and_terminal_are_not_attributed(tmp_path):
    ledger = OperationLedger(tmp_path / "operations.sqlite3")
    harness = GrokBotRecoveryHarness(
        ledger=ledger,
        send_prompt=lambda *args: {
            "delivery": "accepted",
            "messageId": "synthetic-message-id",
        },
        read_thread=lambda route, target, nonce: ThreadObservation(
            correlation_nonce="synthetic-different-nonce",
            reply="synthetic unrelated reply",
            remote_terminal="completed",
            terminal_supported=True,
        ),
    )
    harness.submit(
        route=gateway_route(),
        operation_id=OPERATION_ID,
        client_nonce=NONCE,
        target_identity=TARGET,
        prompt=PROMPT,
    )
    observed = harness.reconcile(
        route=gateway_route(), operation_id=OPERATION_ID
    )

    assert observed.correlation_confirmed is False
    assert observed.reply is None
    assert observed.remote_terminal == "unsupported"
    assert observed.terminal_supported is False
