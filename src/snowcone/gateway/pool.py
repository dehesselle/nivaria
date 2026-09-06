"""A pool of persistent Snowflake connections.

The whole reason this exists: in a corporate account every new connection can
trigger an interactive MFA prompt. Opening N connections eagerly at startup
gets all of those out of the way at once, before the user starts browsing, and
then nothing during a session ever prompts again.

Connections are opened *sequentially*. Opening them in parallel would fan out
N browser windows or N push notifications simultaneously, which is exactly the
disruption this design exists to avoid.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from snowcone.errors import SnowconeError
from snowcone.gateway import queries

EXTERNAL_BROWSER = "externalbrowser"
USERNAME_PASSWORD_MFA = "username_password_mfa"
# The driver's OAuth *authorization code* flow, not the "oauth" authenticator
# that expects an already-obtained token: with no client_id/authorization_url
# override, the driver treats Snowflake itself as the IdP and needs nothing
# from us beyond this string -- it opens a browser and completes the exchange
# on its own, the same shape of interaction as EXTERNAL_BROWSER rather than a
# field the user has to fill in.
OAUTH = "oauth_authorization_code"

AUTHENTICATORS = (OAUTH, EXTERNAL_BROWSER, USERNAME_PASSWORD_MFA)


class ConnectionError_(SnowconeError):
    """Raised when the pool cannot establish its connections."""


@dataclass(frozen=True)
class ConnectParams:
    """Everything needed to open a connection.

    `password` is kept out of repr() so a traceback or a `textual console` log
    cannot leak it, and out of comparisons so that a params object carrying one
    still matches its password-less twin from the recent-connections list.
    """

    account: str
    user: str
    authenticator: str = EXTERNAL_BROWSER
    role: str | None = None
    warehouse: str | None = None
    password: str | None = field(default=None, repr=False, compare=False)

    @property
    def uses_password(self) -> bool:
        return self.authenticator == USERNAME_PASSWORD_MFA

    def to_connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "account": self.account,
            "user": self.user,
            "authenticator": self.authenticator,
            # Without this the session's master token expires mid-browse and
            # the driver silently reauthenticates -- which means a fresh MFA
            # prompt hours into a session. Keeping it alive is what makes
            # "prompt once at startup" actually hold.
            "client_session_keep_alive": True,
            # Let the driver cache the SSO / MFA token, so connections 2..N of
            # the pool usually do not prompt at all.
            "client_store_temporary_credential": True,
            "client_request_mfa_token": True,
        }
        if self.role:
            kwargs["role"] = self.role
        if self.warehouse:
            kwargs["warehouse"] = self.warehouse
        if self.uses_password and self.password:
            kwargs["password"] = self.password
        if self.authenticator == OAUTH:
            # Lets `prepare()` exchange a cached refresh token for a fresh
            # access token silently, instead of opening a browser again --
            # `client_store_temporary_credential` above is what makes that
            # refresh token (and the access token itself) persist between
            # connections and sessions in the first place.
            kwargs["oauth_enable_refresh_tokens"] = True
        return kwargs


class Connector(Protocol):
    """The one call the pool makes into snowflake.connector.

    Injected so the whole pool is testable without the driver or a network.
    """

    def __call__(self, **kwargs: Any) -> Any: ...


def _real_connector(**kwargs: Any) -> Any:
    import snowflake.connector

    return snowflake.connector.connect(**kwargs)


class ConnectionPool:
    """N persistent connections handed out to worker threads.

    A worker that asks for a connection while all N are busy simply blocks on
    `acquire()` -- that is the "stalled worker" state, and it needs no special
    handling beyond the in-use gauge reflecting it.
    """

    def __init__(
        self,
        params: ConnectParams,
        size: int,
        *,
        connector: Connector | None = None,
        on_usage_change: Callable[[int, int], None] | None = None,
    ) -> None:
        if size < 1:
            raise ValueError("pool size must be at least 1")
        self.params = params
        #: How many connections are actually alive right now -- what the Pool
        #: gauge shows as the total. This can fall below `_target_size` when
        #: a background job drops a dead connection rather than replace it.
        self.size = size
        #: What the user asked for. Only `grow()` raises this; a background
        #: shrink never lowers it, so an interactive/visible checkout later
        #: knows there is a gap worth repairing.
        self._target_size = size
        self._connector = connector or _real_connector
        self._on_usage_change = on_usage_change
        self._free: queue.LifoQueue[Any] = queue.LifoQueue()
        self._all: list[Any] = []
        self._in_use = 0
        self._lock = threading.Lock()
        self._closed = False
        #: Held for the whole duration of `run_on_all`, so a session-wide
        #: change (USE ROLE / USE WAREHOUSE) can never overlap a worker's
        #: query on the same connection -- the driver does not support
        #: concurrent cursors on one connection object. `acquire()` waits on
        #: it before every checkout; `run_on_all` holds it while it drains and
        #: rewrites every connection in the pool.
        self._maintenance = threading.Lock()

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    def connect_all(self, progress: Callable[[int, int], None] | None = None) -> None:
        """Open every connection, one at a time, reporting (attempting, total).

        `progress` fires *before* each connection is attempted, not after --
        a caller showing "Connecting 2/3" wants that number up for the whole
        MFA wait the attempt can involve, not just the instant it succeeds.

        Called from a worker thread; `progress` is invoked from that thread, so
        a UI caller must marshal it back to the event loop itself.
        """
        for index in range(self.size):
            if progress is not None:
                progress(index + 1, self.size)
            try:
                connection = self._connector(**self.params.to_connect_kwargs())
            # The driver raises many unrelated exception types; the caller
            # only needs "connecting failed, here is why".
            except Exception as error:
                raise ConnectionError_(str(error)) from error
            self._all.append(connection)
            self._free.put(connection)
        self._adopt_resolved_session()
        self._notify_usage()

    def _adopt_resolved_session(self) -> None:
        """Fill in whatever role/warehouse was left blank with what Snowflake
        actually assumed.

        The driver already knows this for free: its login response carries
        the server-resolved role/warehouse (`sessionInfo.roleName` /
        `.warehouseName`), which `snowflake.connector` exposes as
        `connection.role` / `.warehouse` -- no extra query needed. A session
        always gets *some* role (the user's default, or `PUBLIC`), so "none"
        is never a true answer there; a warehouse can genuinely be absent, so
        this only ever adopts what the connector reports, never invents one.
        An explicitly requested role/warehouse is left untouched.
        """
        if not self._all:
            return
        connection = self._all[0]
        role = self.params.role or getattr(connection, "role", None)
        warehouse = self.params.warehouse or getattr(connection, "warehouse", None)
        if role != self.params.role or warehouse != self.params.warehouse:
            self.params = replace(self.params, role=role, warehouse=warehouse)

    def grow(
        self, additional: int, progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Open `additional` more connections on top of what is already open.

        Same one-at-a-time rule as `connect_all`, for the same reason: this is
        the path taken when the user agrees to more than the single connection
        opened up front, and those prompts must not all land at once either.
        `size` grows as each connection lands, so a caller reading it mid-grow
        (the pool gauge, say) sees the true count rather than the eventual one.

        `progress`, like `connect_all`'s, fires before each attempt rather
        than after, so it stays current for the whole wait an attempt can
        involve.
        """
        if additional < 1:
            raise ValueError("additional must be at least 1")
        target = self.size + additional
        self._target_size = target
        for _ in range(additional):
            if progress is not None:
                progress(self.size + 1, target)
            try:
                connection = self._connector(**self.params.to_connect_kwargs())
            except Exception as error:
                raise ConnectionError_(str(error)) from error
            self._all.append(connection)
            self._free.put(connection)
            self.size += 1
        self._notify_usage()

    @contextlib.contextmanager
    def acquire(
        self, timeout: float | None = None, *, allow_reconnect: bool = True
    ) -> Iterator[Any]:
        """Borrow a connection for the duration of the block.

        Blocks while every connection is busy -- that is the ordinary "stalled
        worker" state and is unaffected by `allow_reconnect`.

        `allow_reconnect` gates whether a *bad* connection may cause a new
        authenticated session to be opened, which for `externalbrowser` can
        mean an interactive MFA prompt:

        - True (the default -- an interactive or visible-priority caller,
          someone actively at the keyboard): a dead connection is replaced
          transparently, at checkout and if the body raises.
        - False (the background staleness sweep, nobody watching): never open
          a new session unattended. A dead connection found at checkout is
          dropped and the pool tries whatever else is already idle instead of
          waiting or reconnecting; a connection that fails mid-use is likewise
          dropped rather than replaced. Either way the pool's advertised
          `size` shrinks to match what is actually alive -- visible on the
          Pool gauge -- and the gap is only repaired later by an
          `allow_reconnect=True` caller that finds the pool short.
        """
        if self._closed:
            raise ConnectionError_("connection pool is closed")
        # A pure barrier: wait for any in-progress run_on_all to finish before
        # taking a connection, so nothing can check one out mid-rewrite.
        with self._maintenance:
            pass
        connection = self._checkout(timeout=timeout, allow_reconnect=allow_reconnect)
        with self._lock:
            self._in_use += 1
        self._notify_usage()
        ok = False
        try:
            yield connection
            ok = True
        finally:
            with self._lock:
                self._in_use -= 1
            if ok:
                self._free.put(connection)
            else:
                self._discard(connection, allow_reconnect=allow_reconnect)
            self._notify_usage()

    def _checkout(self, *, timeout: float | None, allow_reconnect: bool) -> Any:
        connection = self._acquire_free(
            timeout=timeout, allow_reconnect=allow_reconnect
        )
        if not self._is_dead(connection):
            return connection
        if allow_reconnect:
            self._detach(connection)
            return self._open_one()
        # Background priority: skip past dead connections rather than
        # reconnect unattended, but never wait for one that is merely busy
        # elsewhere -- only what is already idle right now is a candidate.
        self._drop(connection)
        while True:
            try:
                candidate = self._free.get_nowait()
            except queue.Empty as error:
                raise ConnectionError_(
                    "no live connection available without reconnecting"
                ) from error
            if not self._is_dead(candidate):
                return candidate
            self._drop(candidate)

    def _acquire_free(self, *, timeout: float | None, allow_reconnect: bool) -> Any:
        """Dequeue an idle connection -- this is also where a slot a
        background job dropped earlier gets repaired.

        A background job never opens a connection here: if none are alive at
        all, waiting would hang forever (nothing left to ever free one up),
        so that fails fast instead; if some are merely busy, blocking is the
        ordinary, harmless "stalled worker" state and is left alone.

        An interactive/visible caller (`allow_reconnect=True`) may find `size`
        short of what growth last established (`_target_size`) because a
        background job dropped a dead connection rather than replace it. If
        nothing is idle to dequeue in that case, it opens the missing
        connection itself right now, rather than waiting on a slot that may
        never be handed back.
        """
        with self._lock:
            live = self.size
        if not allow_reconnect:
            if live == 0:
                raise ConnectionError_("connection pool is empty")
            return self._free.get(timeout=timeout)
        with self._lock:
            short = self.size < self._target_size
        if not short:
            return self._free.get(timeout=timeout)
        try:
            return self._free.get_nowait()
        except queue.Empty:
            connection = self._open_one()
            with self._lock:
                self.size += 1
            self._notify_usage()
            return connection

    @staticmethod
    def _is_dead(connection: Any) -> bool:
        is_closed = getattr(connection, "is_closed", None)
        return is_closed is not None and is_closed()

    def _detach(self, connection: Any) -> None:
        """Close a bad connection and remove it from the live set."""
        with contextlib.suppress(Exception):
            connection.close()
        with self._lock:
            for index, existing in enumerate(self._all):
                if existing is connection:
                    del self._all[index]
                    break

    def _open_one(self) -> Any:
        connection = self._connector(**self.params.to_connect_kwargs())
        with self._lock:
            self._all.append(connection)
        return connection

    def _drop(self, connection: Any) -> None:
        """Detach a bad connection and shrink `size` to match -- no replacement."""
        self._detach(connection)
        with self._lock:
            self.size -= 1
        self._notify_usage()

    def _discard(self, connection: Any, *, allow_reconnect: bool) -> None:
        """A connection that just failed mid-use is never handed out again.

        Mirrors the checkout-time policy: replaced in place (and queued for
        the next caller) when reconnecting is allowed, otherwise dropped and
        the pool shrinks -- repaired later by an interactive/visible caller.
        """
        self._detach(connection)
        if allow_reconnect:
            self._free.put(self._open_one())
        else:
            with self._lock:
                self.size -= 1

    def run_on_all(self, statement: str, *, timeout: float = 30.0) -> None:
        """Run a session-scoped statement (USE ROLE / USE WAREHOUSE) against
        every pooled connection, so a later query cannot land on a connection
        that still carries the old session state.

        Every currently live connection is drained from the free queue first
        -- blocking, like any other checkout, until whatever is mid-query
        finishes and returns it -- and only put back once the statement has
        run on all of them. Held via `_maintenance` throughout, which is what
        stops `acquire()` from handing one of them to a worker half-way
        through. A connection found dead while draining is replaced the same
        way `acquire()` would.

        Not transactional across connections: if the statement fails on one
        (a typo'd role, say) after already succeeding on another, the pool is
        left with a real split and the caller sees the error rather than a
        silent partial switch. Full atomicity would need Snowflake's
        cooperation across sessions, which does not exist -- and the common
        failure here is "no such role", which the picker already guards
        against by only ever offering roles SHOW ROLES actually returned.
        """
        with self._maintenance:
            with self._lock:
                expected = self.size
            held: list[Any] = []
            try:
                for _ in range(expected):
                    try:
                        connection = self._free.get(timeout=timeout)
                    except queue.Empty as error:
                        raise ConnectionError_(
                            "timed out waiting for a connection to go idle"
                        ) from error
                    if self._is_dead(connection):
                        self._detach(connection)
                        connection = self._open_one()
                    held.append(connection)
                for connection in held:
                    cursor = connection.cursor()
                    try:
                        cursor.execute(statement)
                    finally:
                        cursor.close()
            finally:
                for connection in held:
                    self._free.put(connection)

    def set_role(self, role: str) -> None:
        """Switch every pooled connection's role.

        SHOW results are filtered by the session's role, so every connection
        has to agree -- otherwise which role served a given query would
        depend on which pool connection happened to be free.
        """
        self.run_on_all(queries.use_role(role))
        self.params = replace(self.params, role=role)

    def set_warehouse(self, warehouse: str) -> None:
        """Switch every pooled connection's warehouse. See `set_role`."""
        self.run_on_all(queries.use_warehouse(warehouse))
        self.params = replace(self.params, warehouse=warehouse)

    def close(self) -> None:
        self._closed = True
        for connection in self._all:
            with contextlib.suppress(Exception):
                connection.close()
        self._all.clear()

    def _notify_usage(self) -> None:
        if self._on_usage_change is not None:
            self._on_usage_change(self.in_use, self.size)
