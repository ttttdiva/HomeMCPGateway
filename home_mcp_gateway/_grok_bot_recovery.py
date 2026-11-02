"""Internal recovery harness for the separate Grok Bot transport.

This module is deliberately not registered as a Gateway or MCP tool. Callers
must adapt the existing Grok Bot app-session and gateway functions to the
callback interfaces below. Credentials and recovered route data stay in
memory; only operation identity metadata is written to the caller-selected
SQLite ledger.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal
from urllib.parse import urlsplit
import uuid


ENSURE_SANDBOX_ENDPOINT = (
    "https://api2.cursor.sh/aiserver.v1.GrokBotService/EnsureSandBox"
)
_OPERATION_STATES = {"prepared", "sending", "accepted", "unknown", "rejected"}
_IDENTIFIER_LIMIT = 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class RecoveryHarnessError(RuntimeError):
    """Base error for local recovery policy violations."""


class AccountMismatchError(RecoveryHarnessError):
    """The active app account is not the account approved by the caller."""


class TeamMismatchError(RecoveryHarnessError):
    """The selected team differs from the one pinned to the operation."""


class EndpointPolicyError(RecoveryHarnessError):
    """A credential-bearing request or route failed the URL policy."""


class OperationConflictError(RecoveryHarnessError):
    """An operation ID was reused with different immutable input."""


class OperationBindingMismatchError(RecoveryHarnessError):
    """Correlation evidence does not match the operation's immutable binding."""


class OperationCorrelationError(RecoveryHarnessError):
    """Correlation evidence is incomplete or malformed."""


class OperationAnchorError(RecoveryHarnessError):
    """An operation is not bound to its required previously approved row."""


class NonceConflictError(RecoveryHarnessError):
    """A client nonce was already reserved by a different operation."""


class EnsureSandboxRejected(RecoveryHarnessError):
    """EnsureSandBox did not return a successful route response."""


@dataclass(frozen=True)
class ExistingAccount:
    """Identity of the currently active Grok Bot app account.

    The adapter that constructs this value must use the current Windows user
    and the app's existing account/session mechanism. This value intentionally
    contains no credential.
    """

    identity: str = field(repr=False)
    active: bool = False


@dataclass(frozen=True)
class EnsureSandboxRequest:
    """Request contract for the existing account's EnsureSandBox adapter."""

    account_identity: str = field(repr=False)
    endpoint: str = ENSURE_SANDBOX_ENDPOINT
    redirect_policy: Literal["error"] = "error"


@dataclass(frozen=True)
class EnsureSandboxResponse:
    """Sanitized fields required from the EnsureSandBox adapter.

    ``redirect_history`` must be taken from the underlying response object's
    actual history. Adapters must use a no-follow transport; they must not
    synthesize an empty history after following redirects.
    """

    account_identity: str = field(repr=False)
    status_code: int
    response_url: str
    gateway_url: str
    gateway_token: str = field(repr=False)
    redirect_history: tuple[str, ...]
    gateway_headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class GatewayRoute:
    """Transient route data; repr hides the token and headers."""

    account_identity: str = field(repr=False)
    gateway_url: str
    gateway_token: str = field(repr=False)
    gateway_headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    account_identity: str
    client_nonce: str
    target_identity: str
    prompt_sha256: str
    transport_state: str
    team_identity: str = "unbound"
    request_message_id: str | None = None
    request_id: str | None = None
    root_id: str | None = None


@dataclass(frozen=True)
class ThreadObservation:
    """Normalized read-only evidence from the existing getThread path.

    The adapter must only read the target thread. It must not send or replay a
    prompt. Terminal tracking defaults to unsupported until an adapter can
    prove otherwise from the upstream contract. ``correlation_nonce`` must be
    extracted from matching remote thread evidence; echoing the requested
    nonce without matching a thread entry is not correlation evidence.
    """

    correlation_nonce: str | None = field(default=None, repr=False)
    reply: Any = field(default=None, repr=False)
    remote_terminal: Any = None
    terminal_supported: bool = False
    request_id_observed: bool = False
    root_id_observed: bool = False
    error_stage: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class OperationOutcome:
    operation_id: str
    transport_status: Literal["accepted", "unknown", "rejected"]
    reply: Any = field(default=None, repr=False)
    remote_terminal: Any = "unsupported"
    terminal_supported: bool = False
    reconciled: bool = False
    correlation_confirmed: bool = False
    send_error: BaseException | None = field(default=None, repr=False)
    reconciliation_error: BaseException | None = field(default=None, repr=False)


def _validate_identifier(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _IDENTIFIER_LIMIT
        or _CONTROL_CHARS.search(value)
    ):
        raise ValueError(f"{label} must be a non-empty stable identifier.")
    return value


def validate_ensure_sandbox_endpoint(endpoint: str) -> str:
    """Accept only the reviewed official endpoint, with no URL variations."""

    if endpoint != ENSURE_SANDBOX_ENDPOINT:
        raise EndpointPolicyError("EnsureSandBox endpoint is outside the allowlist.")
    try:
        parsed = urlsplit(endpoint)
    except ValueError as error:
        raise EndpointPolicyError("EnsureSandBox endpoint is invalid.") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api2.cursor.sh"
        or parsed.path != "/aiserver.v1.GrokBotService/EnsureSandBox"
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EndpointPolicyError("EnsureSandBox endpoint is outside the allowlist.")
    return endpoint


def validate_gateway_url(raw_url: str) -> str:
    """Mirror the CLI gateway host allowlist without environment overrides."""

    try:
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").lower()
        # Accessing .port validates malformed port text.
        parsed.port
    except (TypeError, ValueError) as error:
        raise EndpointPolicyError("Recovered gateway URL is invalid.") from error

    api_host = (
        host in {"cursor.sh", "cursor.com"}
        or host.endswith(".cursor.sh")
        or host.endswith(".cursor.com")
    )
    gateway_host = api_host or host.endswith(".cursorvm.com")
    if (
        parsed.scheme != "https"
        or not gateway_host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise EndpointPolicyError("Recovered gateway URL is outside the allowlist.")
    return raw_url[:-1] if raw_url.endswith("/") else raw_url


def recover_existing_account_route(
    *,
    account: ExistingAccount,
    expected_account_identity: str,
    descriptor_state: Literal["absent", "asleep", "ready"],
    existing_route: GatewayRoute | None,
    ensure_sandbox: Callable[[EnsureSandboxRequest], EnsureSandboxResponse],
) -> GatewayRoute:
    """Return an in-memory route, recovering absent/asleep routes fail-closed.

    ``ensure_sandbox`` is an adapter over the existing Grok Bot account path.
    It must obtain the active account's credential through that app's existing
    mechanism and perform only EnsureSandBox at the request endpoint, with
    redirects rejected. This helper never receives or persists that credential.
    """

    expected = _validate_identifier(expected_account_identity, "Expected account")
    current = _validate_identifier(account.identity, "Active account")
    if account.active is not True or current != expected:
        raise AccountMismatchError(
            "The active Grok Bot account does not match the approved account."
        )

    if descriptor_state == "ready":
        if existing_route is None:
            raise RecoveryHarnessError("A ready descriptor has no route data.")
        if existing_route.account_identity != current:
            raise AccountMismatchError("The route belongs to a different Grok Bot account.")
        url = validate_gateway_url(existing_route.gateway_url)
        if not existing_route.gateway_token:
            raise RecoveryHarnessError("The existing route is incomplete.")
        return GatewayRoute(
            account_identity=current,
            gateway_url=url,
            gateway_token=existing_route.gateway_token,
            gateway_headers=existing_route.gateway_headers,
        )

    if descriptor_state not in {"absent", "asleep"}:
        raise RecoveryHarnessError("Unsupported descriptor state.")

    endpoint = validate_ensure_sandbox_endpoint(ENSURE_SANDBOX_ENDPOINT)
    response = ensure_sandbox(
        EnsureSandboxRequest(
            account_identity=current,
            endpoint=endpoint,
            redirect_policy="error",
        )
    )
    redirect_history = response.redirect_history
    if not isinstance(redirect_history, tuple) or any(
        not isinstance(url, str) or not url for url in redirect_history
    ):
        raise EndpointPolicyError("EnsureSandBox redirect history is unavailable.")
    if (
        redirect_history
        or 300 <= response.status_code < 400
        or response.response_url != endpoint
    ):
        raise EndpointPolicyError("EnsureSandBox redirect was rejected.")
    if not 200 <= response.status_code < 300:
        raise EnsureSandboxRejected("EnsureSandBox returned an unsuccessful response.")
    if response.account_identity != current:
        raise AccountMismatchError(
            "EnsureSandBox used a different Grok Bot account context."
        )
    if not response.gateway_token:
        raise RecoveryHarnessError("EnsureSandBox returned an incomplete route.")

    return GatewayRoute(
        account_identity=current,
        gateway_url=validate_gateway_url(response.gateway_url),
        gateway_token=response.gateway_token,
        gateway_headers=response.gateway_headers,
    )


class OperationLedger:
    """Atomic local ledger for exactly-once *attempt* handling.

    Prompt text, replies, route data, and credentials are never stored. The
    caller must choose a private ledger path appropriate for the user's
    environment. SQLite commits use DELETE journaling and FULL synchronization.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.exists() and self.path.is_dir():
            raise IsADirectoryError(self.path)
        if not self.path.parent.exists():
            raise FileNotFoundError(self.path.parent)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    account_identity TEXT NOT NULL,
                    team_identity TEXT NOT NULL DEFAULT 'unbound',
                    client_nonce TEXT NOT NULL UNIQUE,
                    target_identity TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    transport_state TEXT NOT NULL,
                    request_message_id TEXT,
                    request_id TEXT,
                    root_id TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(operations)")
            }
            if "account_identity" not in columns:
                raise RecoveryHarnessError(
                    "Operation ledger schema lacks account binding."
                )
            if "team_identity" not in columns:
                connection.execute(
                    "ALTER TABLE operations ADD COLUMN team_identity TEXT NOT NULL DEFAULT 'unbound'"
                )
            for column in ("request_message_id", "request_id", "root_id"):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE operations ADD COLUMN {column} TEXT"
                    )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=15000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OperationRecord:
        columns = row.keys()
        return OperationRecord(
            operation_id=row["operation_id"],
            account_identity=row["account_identity"],
            client_nonce=row["client_nonce"],
            target_identity=row["target_identity"],
            prompt_sha256=row["prompt_sha256"],
            transport_state=row["transport_state"],
            team_identity=row["team_identity"],
            request_message_id=(
                row["request_message_id"]
                if "request_message_id" in columns
                else None
            ),
            request_id=row["request_id"] if "request_id" in columns else None,
            root_id=row["root_id"] if "root_id" in columns else None,
        )

    def reserve(
        self,
        *,
        operation_id: str,
        account_identity: str,
        client_nonce: str,
        target_identity: str,
        prompt: str,
        team_identity: str = "unbound",
    ) -> tuple[OperationRecord, bool]:
        operation_id = _validate_identifier(operation_id, "Operation ID")
        account_identity = _validate_identifier(account_identity, "Account identity")
        client_nonce = _validate_identifier(client_nonce, "Client nonce")
        target_identity = _validate_identifier(target_identity, "Target identity")
        team_identity = _validate_identifier(team_identity, "Team identity")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string.")
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is not None:
                existing = self._from_row(row)
                if existing.account_identity != account_identity:
                    raise AccountMismatchError(
                        "Operation belongs to a different Grok Bot account."
                    )
                if existing.team_identity != team_identity:
                    raise TeamMismatchError(
                        "Operation belongs to a different Grok Bot team."
                    )
                if (
                    existing.client_nonce != client_nonce
                    or existing.target_identity != target_identity
                    or existing.prompt_sha256 != prompt_hash
                ):
                    raise OperationConflictError(
                        "Operation ID was already reserved with different input."
                    )
                return existing, False

            nonce_row = connection.execute(
                "SELECT operation_id FROM operations WHERE client_nonce = ?",
                (client_nonce,),
            ).fetchone()
            if nonce_row is not None:
                raise NonceConflictError("Client nonce is already reserved.")

            connection.execute(
                """
                INSERT INTO operations (
                    operation_id, account_identity, team_identity, client_nonce,
                    target_identity, prompt_sha256, transport_state
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared')
                """,
                (
                    operation_id,
                    account_identity,
                    team_identity,
                    client_nonce,
                    target_identity,
                    prompt_hash,
                ),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            return self._from_row(row), True

    def begin_send(
        self,
        *,
        operation_id: str,
        account_identity: str,
        team_identity: str,
        target_identity: str,
        prompt: str,
        required_anchor_operation_id: str | None = None,
        required_anchor_prompt_sha256: str | None = None,
    ) -> tuple[OperationRecord, bool]:
        """Atomically admit one send with a durable UUID nonce.

        The row is committed in ``sending`` state before the caller enters its
        transport. Existing rows are returned read-only, including rows left
        in ``sending`` after a crash.
        """

        operation_id = _validate_identifier(operation_id, "Operation ID")
        account_identity = _validate_identifier(account_identity, "Account identity")
        team_identity = _validate_identifier(team_identity, "Team identity")
        target_identity = _validate_identifier(target_identity, "Target identity")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string.")
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if required_anchor_operation_id is not None:
            required_anchor_operation_id = _validate_identifier(
                required_anchor_operation_id, "Anchor operation ID"
            )
            if (
                required_anchor_operation_id == operation_id
                or not isinstance(required_anchor_prompt_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", required_anchor_prompt_sha256)
            ):
                raise OperationAnchorError("MARKER_ANCHOR_INVALID")

        with self._transaction() as connection:
            if required_anchor_operation_id is not None:
                anchor = connection.execute(
                    "SELECT account_identity,team_identity,target_identity,prompt_sha256,transport_state "
                    "FROM operations WHERE operation_id = ?",
                    (required_anchor_operation_id,),
                ).fetchone()
                if (
                    anchor is None
                    or anchor["prompt_sha256"] != required_anchor_prompt_sha256
                    or anchor["transport_state"] != "accepted"
                ):
                    raise OperationAnchorError("MARKER_ANCHOR_INVALID")
                if (
                    anchor["account_identity"] != account_identity
                    or anchor["team_identity"] != team_identity
                    or anchor["target_identity"] != target_identity
                ):
                    raise OperationAnchorError("MARKER_ANCHOR_MISMATCH")

            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is not None:
                existing = self._from_row(row)
                if existing.account_identity != account_identity:
                    raise AccountMismatchError(
                        "Operation belongs to a different Grok Bot account."
                    )
                if existing.team_identity != team_identity:
                    raise TeamMismatchError(
                        "Operation belongs to a different Grok Bot team."
                    )
                if (
                    existing.target_identity != target_identity
                    or existing.prompt_sha256 != prompt_hash
                ):
                    raise OperationConflictError(
                        "Operation ID was already admitted with different input."
                    )
                return existing, False

            client_nonce = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO operations (
                    operation_id, account_identity, team_identity, client_nonce,
                    target_identity, prompt_sha256, transport_state
                ) VALUES (?, ?, ?, ?, ?, ?, 'sending')
                """,
                (
                    operation_id,
                    account_identity,
                    team_identity,
                    client_nonce,
                    target_identity,
                    prompt_hash,
                ),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            return self._from_row(row), True

    def get(self, operation_id: str) -> OperationRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def set_transport_state(
        self,
        operation_id: str,
        state: str,
        *,
        correlation: Mapping[str, str] | None = None,
    ) -> OperationRecord:
        if state not in _OPERATION_STATES:
            raise ValueError("Unsupported operation transport state.")
        if correlation is not None:
            expected_correlation_keys = {
                "account_identity",
                "team_identity",
                "target_identity",
                "client_nonce",
                "prompt_sha256",
                "request_message_id",
                "request_id",
                "root_id",
            }
            if set(correlation) != expected_correlation_keys:
                raise OperationCorrelationError("Operation correlation is incomplete.")
            for key in expected_correlation_keys:
                _validate_identifier(correlation[key], key.replace("_", " ").title())
            if not re.fullmatch(r"[0-9a-f]{64}", correlation["prompt_sha256"]):
                raise OperationCorrelationError("Operation prompt hash is invalid.")

        with self._transaction() as connection:
            current = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if current is None:
                raise KeyError(operation_id)
            record = self._from_row(current)
            if correlation is not None:
                for key in (
                    "account_identity",
                    "team_identity",
                    "target_identity",
                    "client_nonce",
                    "prompt_sha256",
                ):
                    if getattr(record, key) != correlation[key]:
                        raise OperationBindingMismatchError(
                            "Operation correlation binding mismatch."
                        )
                existing_ids = (
                    record.request_message_id,
                    record.request_id,
                    record.root_id,
                )
                if any(value is not None for value in existing_ids):
                    if not all(value is not None for value in existing_ids):
                        raise OperationCorrelationError("Operation correlation is incomplete.")
                    if existing_ids != (
                        correlation["request_message_id"],
                        correlation["request_id"],
                        correlation["root_id"],
                    ):
                        raise OperationConflictError(
                            "Operation correlation IDs conflict with the existing record."
                        )

            current_state = record.transport_state
            if current_state == "accepted" and state != "accepted":
                next_state = current_state
            elif current_state == "rejected" and state not in {"rejected", "accepted"}:
                next_state = current_state
            else:
                next_state = state

            if correlation is None:
                cursor = connection.execute(
                    "UPDATE operations SET transport_state = ? WHERE operation_id = ?",
                    (next_state, operation_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE operations
                    SET transport_state = ?, request_message_id = ?,
                        request_id = ?, root_id = ?
                    WHERE operation_id = ?
                    """,
                    (
                        next_state,
                        correlation["request_message_id"],
                        correlation["request_id"],
                        correlation["root_id"],
                        operation_id,
                    ),
                )
            if cursor.rowcount != 1:
                raise KeyError(operation_id)
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            return self._from_row(row)


class GrokBotRecoveryHarness:
    """Wrap the existing sendPrompt/getThread adapters without replaying sends."""

    def __init__(
        self,
        *,
        ledger: OperationLedger,
        send_prompt: Callable[[GatewayRoute, str, str, Mapping[str, str]], Any],
        read_thread: Callable[[GatewayRoute, str, str], ThreadObservation],
    ) -> None:
        self._ledger = ledger
        self._send_prompt = send_prompt
        self._read_thread = read_thread

    @staticmethod
    def _receipt_status(receipt: Any) -> Literal["accepted", "unknown"]:
        if isinstance(receipt, Mapping):
            delivery = receipt.get("delivery")
            message_id = receipt.get("messageId")
        else:
            delivery = getattr(receipt, "delivery", None)
            message_id = getattr(receipt, "messageId", None)
        return (
            "accepted"
            if delivery == "accepted"
            and isinstance(message_id, str)
            and bool(message_id)
            else "unknown"
        )

    @staticmethod
    def _error_status(error: BaseException) -> Literal["rejected", "unknown"]:
        return "rejected" if getattr(error, "delivery", None) == "rejected" else "unknown"

    @staticmethod
    def _without_thread(
        record: OperationRecord,
        status: Literal["accepted", "unknown", "rejected"],
        *,
        send_error: BaseException | None = None,
    ) -> OperationOutcome:
        return OperationOutcome(
            operation_id=record.operation_id,
            transport_status=status,
            remote_terminal="unsupported",
            terminal_supported=False,
            send_error=send_error,
        )

    def _read_only_reconciliation(
        self,
        record: OperationRecord,
        route: GatewayRoute,
        *,
        send_error: BaseException | None = None,
    ) -> OperationOutcome:
        try:
            observation = self._read_thread(
                route, record.target_identity, record.client_nonce
            )
            if not isinstance(observation, ThreadObservation):
                raise TypeError("Thread adapter must return ThreadObservation.")
        except Exception as error:
            return OperationOutcome(
                operation_id=record.operation_id,
                transport_status=(
                    record.transport_state
                    if record.transport_state in {"accepted", "rejected"}
                    else "unknown"
                ),
                remote_terminal="unsupported",
                terminal_supported=False,
                reconciled=False,
                send_error=send_error,
                reconciliation_error=error,
            )

        correlated = observation.correlation_nonce == record.client_nonce
        request_confirmed = (
            correlated
            and observation.request_id_observed
            and observation.root_id_observed
        )
        if request_confirmed and record.transport_state in {
            "prepared",
            "sending",
            "unknown",
        }:
            record = self._ledger.set_transport_state(
                record.operation_id, "accepted"
            )
        reconciliation_error = (
            RuntimeError(observation.error_code or "reconciliation_failed")
            if observation.error_stage is not None
            else None
        )
        return OperationOutcome(
            operation_id=record.operation_id,
            transport_status=(
                record.transport_state
                if record.transport_state in {"accepted", "rejected"}
                else "unknown"
            ),
            reply=observation.reply if correlated else None,
            remote_terminal=(
                observation.remote_terminal
                if correlated and observation.terminal_supported
                else "unsupported"
            ),
            terminal_supported=correlated and observation.terminal_supported,
            reconciled=observation.error_stage is None,
            correlation_confirmed=request_confirmed,
            send_error=send_error,
            reconciliation_error=reconciliation_error,
        )

    def submit(
        self,
        *,
        route: GatewayRoute,
        operation_id: str,
        client_nonce: str,
        target_identity: str,
        prompt: str,
        team_identity: str = "unbound",
    ) -> OperationOutcome:
        """Send once after reservation; target_identity must be a resolved ID.

        ``send_prompt`` must map ``extra["clientNonce"]`` to the existing
        ``sendPrompt(..., extra)`` API. Duplicate calls only read the thread.
        """

        record, created = self._ledger.reserve(
            operation_id=operation_id,
            account_identity=route.account_identity,
            client_nonce=client_nonce,
            target_identity=target_identity,
            prompt=prompt,
            team_identity=team_identity,
        )
        if not created:
            return self._read_only_reconciliation(record, route)

        # Persist the possible-send boundary before entering the transport.
        record = self._ledger.set_transport_state(operation_id, "sending")
        try:
            receipt = self._send_prompt(
                route,
                record.target_identity,
                prompt,
                {"clientNonce": record.client_nonce},
            )
        except Exception as error:
            status = self._error_status(error)
            record = self._ledger.set_transport_state(operation_id, status)
            if status == "unknown":
                return self._read_only_reconciliation(
                    record, route, send_error=error
                )
            return self._without_thread(record, status, send_error=error)

        status = self._receipt_status(receipt)
        record = self._ledger.set_transport_state(operation_id, status)
        if status == "unknown":
            return self._read_only_reconciliation(record, route)
        # An accepted receipt does not establish a reply or remote terminal.
        return self._without_thread(record, status)

    def reconcile(
        self, *, route: GatewayRoute, operation_id: str
    ) -> OperationOutcome:
        """Read the thread for a prior operation; this method cannot send."""

        record = self._ledger.get(operation_id)
        if record is None:
            raise KeyError(operation_id)
        route_account = _validate_identifier(
            route.account_identity, "Route account"
        )
        if route_account != record.account_identity:
            raise AccountMismatchError(
                "Operation belongs to a different Grok Bot account."
            )
        return self._read_only_reconciliation(record, route)
