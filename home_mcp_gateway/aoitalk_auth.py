"""Gateway-lifespan-owned AoiTalk authentication reuse.

One in-memory, process- and event-loop-local authenticated transport per
validated operator configuration. Nothing is persisted: the session cookie
lives only in the owned ``httpx.AsyncClient`` jar. Outside an active owner
(tests, other loops, scripts) adapters keep their per-call login behaviour.

Contract: authentication is single-flight; a login 429 starts a monotonic
per-identity cooldown from its validated ``Retry-After`` and callers inside it
get the same blocked error without a new login; a 401 invalidates the session;
no operation is ever replayed after a failure.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import math
import time
from typing import Any, Awaitable, Callable

from . import aoitalk

_OWNER: "AuthOwner | None" = None
UNKNOWN_RETRY_AFTER_SECONDS = 60


class _Entry:
    def __init__(self, settings: aoitalk.Settings, client: aoitalk.Client):
        self.settings = settings
        self.client = client
        self.users = 0
        self.retired = False


class AuthOwner:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._entry: _Entry | None = None
        self._inflight: tuple[aoitalk.Settings, asyncio.Future] | None = None
        # identity -> (monotonic expiry, True if the delay was advertised by the server)
        self._cooldowns: dict[tuple[str, str], tuple[float, bool]] = {}
        self._retired: list[_Entry] = []
        self._pending: aoitalk.Client | None = None
        self._closed = False

    @staticmethod
    def _identity(settings: aoitalk.Settings) -> tuple[str, str]:
        return settings.base_url, settings.username

    def _blocked(self, settings: aoitalk.Settings) -> None:
        hold = self._cooldowns.get(self._identity(settings))
        if hold is None:
            return
        remaining = hold[0] - time.monotonic()
        if remaining <= 0:
            del self._cooldowns[self._identity(settings)]
            return
        seconds = math.ceil(remaining)
        if hold[1]:
            raise aoitalk.ClipError("rate_limited", 429, seconds)
        raise aoitalk.ClipError("rate_limited", 429, local_hold=seconds)

    def note_rate_limited(self, settings: aoitalk.Settings, retry_after: int | None) -> None:
        """Start the per-identity cooldown after an API 429 (validated or bounded default)."""
        advertised = bool(retry_after)
        hold = retry_after if advertised else UNKNOWN_RETRY_AFTER_SECONDS
        now = time.monotonic()
        until = now + hold
        key = self._identity(settings)
        existing = self._cooldowns.get(key)
        if existing is not None and existing[0] > now:
            # Concurrent responses can arrive out of order: never shorten a live hold.
            if existing[0] > until:
                return
            if existing[0] == until:
                advertised = advertised or existing[1]
        self._cooldowns[key] = (until, advertised)

    async def _retire(self, entry: _Entry) -> None:
        entry.retired = True
        if entry.users == 0:
            await entry.client.aclose()
        else:
            self._retired.append(entry)

    async def _login(self, settings: aoitalk.Settings) -> _Entry:
        if self._closed:  # shutdown may have begun while the old client was being retired
            raise aoitalk.ClipError("connection_error")
        client = aoitalk.Client(settings)
        self._pending = client
        try:
            await client.login()
            if self._closed:  # shutdown raced the login: never install a session
                raise aoitalk.ClipError("connection_error")
        except RuntimeError:  # httpx: client closed by shutdown while logging in
            await client.aclose()
            raise aoitalk.ClipError("connection_error") from None
        except aoitalk.ClipError as exc:
            await client.aclose()
            if exc.http_status == 429 and not self._closed:
                # Absent/malformed Retry-After still holds a bounded cooldown so callers
                # cannot loop on logins; 60 s is the server's own maximum failure backoff.
                self.note_rate_limited(settings, exc.retry_after)
            raise
        except BaseException:
            await client.aclose()
            raise
        finally:
            self._pending = None
        return _Entry(settings, client)

    async def _authenticated(self, settings: aoitalk.Settings) -> _Entry:
        while True:
            if self._closed:
                raise aoitalk.ClipError("connection_error")
            self._blocked(settings)  # any 429 cooldown applies before reusing a session too
            entry = self._entry
            if entry is not None and entry.settings == settings:
                return entry
            if self._inflight is not None:
                flight_settings, future = self._inflight
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    raise
                except aoitalk.ClipError as exc:
                    if flight_settings == settings:
                        raise aoitalk.ClipError(exc.code, exc.http_status, exc.retry_after,
                                                exc.local_hold) from None
                continue
            future: asyncio.Future = self.loop.create_future()
            future.add_done_callback(lambda f: f.cancelled() or f.exception())
            self._inflight = (settings, future)  # covers retirement and login as one transition
            try:
                if entry is not None:  # changed endpoint/identity/credentials: never reuse the old jar
                    self._entry = None
                    await self._retire(entry)
                new = await self._login(settings)
                if self._closed:
                    await new.client.aclose()
                    raise aoitalk.ClipError("connection_error")
                self._entry = new
                future.set_result(None)
            except BaseException as exc:
                future.set_exception(exc if isinstance(exc, aoitalk.ClipError)
                                     else aoitalk.ClipError("connection_error"))
                raise
            finally:
                self._inflight = None
            return new

    @asynccontextmanager
    async def session(self, settings: aoitalk.Settings):
        entry = await self._authenticated(settings)
        entry.users += 1
        try:
            yield entry.client.http
        finally:
            entry.users -= 1
            if entry.retired and entry.users == 0:
                if entry in self._retired:
                    self._retired.remove(entry)
                await entry.client.aclose()

    async def invalidate(self, http: Any) -> None:
        entry = self._entry
        if entry is not None and entry.client.http is http:
            self._entry = None
            await self._retire(entry)

    async def aclose(self) -> None:
        self._closed = True
        if self._pending is not None:
            await self._pending.aclose()  # abort an in-progress login
        if self._inflight is not None:
            try:  # join the transition; its result is never installed
                await asyncio.shield(self._inflight[1])
            except BaseException:
                pass
        entries = [e for e in [self._entry, *self._retired] if e is not None]
        self._entry, self._retired = None, []
        for entry in entries:
            entry.retired = True
            await entry.client.aclose()


async def activate() -> AuthOwner:
    """Install the owner for the running (Gateway lifespan) event loop."""
    global _OWNER
    if _OWNER is not None:
        await _OWNER.aclose()
    _OWNER = AuthOwner()
    return _OWNER


async def deactivate() -> None:
    global _OWNER
    owner, _OWNER = _OWNER, None
    if owner is not None:
        await owner.aclose()


def _active_owner() -> AuthOwner | None:
    owner = _OWNER
    if owner is None or owner._closed:
        return None
    try:
        return owner if asyncio.get_running_loop() is owner.loop else None
    except RuntimeError:
        return None


@asynccontextmanager
async def client_for(cls: type, settings: aoitalk.Settings,
                     login: Callable[[Any], Awaitable[None]],
                     convert: Callable[[aoitalk.ClipError], Exception],
                     unauthorized: Callable[[Exception], bool]):
    """Yield an adapter client: shared authenticated transport if an owner is active.

    ``login`` is the adapter's own per-call login (fallback path only);
    ``convert`` maps an authentication ClipError to the adapter error;
    ``unauthorized`` recognises the adapter's 401 so the session is dropped.
    """
    owner = _active_owner()
    if owner is None:
        client = cls(settings)
        try:
            await login(client)
            yield client
        finally:
            await client.aclose()
        return
    async with AsyncExitStack() as stack:
        try:
            http = await stack.enter_async_context(owner.session(settings))
        except aoitalk.ClipError as exc:
            raise convert(exc) from None
        try:
            yield cls(settings, http=http)
        except Exception as exc:
            if unauthorized(exc):
                await owner.invalidate(http)
            elif getattr(exc, "http_status", None) == 429:  # HTTP status, not the (overridable) error code
                owner.note_rate_limited(settings, getattr(exc, "retry_after", None))
            raise
