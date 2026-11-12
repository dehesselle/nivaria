# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

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
import logging
import os
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from nivaria import journal
from nivaria.snowflake import queries, sql_log

_log = logging.getLogger(__name__)

#: Replaces the displayed account name, for screenshots: an SVG's text cannot be
#: blurred. An environment variable rather than a setting, so that it cannot
#: outlive the run and leave a real account showing a made-up name.
ACCOUNT_LABEL_ENV = "NIVARIA_ACCOUNT_LABEL"

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

#: How long a single connection attempt waits for browser/MFA approval before
#: giving up. Deliberately short and fixed rather than a Setting -- a tuning
#: knob for "how long should a denied prompt take to resolve," not a per-user
#: preference. Bounds OAuth's and password+MFA's approval wait (see
#: docs/DESIGN.md, "The connection pool"); externalbrowser's local-socket wait is
#: not governed by either kwarg in the installed driver (verified: no
#: timeout exists anywhere in that code path) and stays unbounded -- an
#: accepted gap, also called out there.
CONNECT_TIMEOUT_SECONDS = 20

#: What a browser login that did not finish reports, for errno 251016. Nearly
#: always nobody answered the prompt, which the driver's own text sends looking
#: for a redirect URI problem. That can be the cause, so this does not claim the
#: prompt went unanswered. Spelled out because this module never imports the
#: driver.
_LOGIN_TIMEOUT_ERRNO = 251016
LOGIN_TIMED_OUT = "the browser login did not finish in time"


def _login_failure_text(error: Exception) -> str:
    """What to tell the user about a failed login. The log gets the driver's."""
    _log.warning("a login failed: %s", error)
    journal.connection(f"login failed: {error}")
    if getattr(error, "errno", None) == _LOGIN_TIMEOUT_ERRNO:
        return LOGIN_TIMED_OUT
    return str(error)


#: How often a worker waiting for a connection looks again at whether one can
#: still come. A given-up connection puts nothing on the queue to wake it.
_WAIT_RECHECK_SECONDS = 0.5


class ConnectionError_(Exception):
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
            # Bounds how long a denied or abandoned browser/MFA approval
            # leaves the "Connecting ..." dialog stuck -- see
            # CONNECT_TIMEOUT_SECONDS above for which authenticators this
            # actually governs.
            "login_timeout": CONNECT_TIMEOUT_SECONDS,
            "external_browser_timeout": CONNECT_TIMEOUT_SECONDS,
            # Lets an admin attribute sessions to nivaria (it lands in
            # SESSIONS.CLIENT_ENVIRONMENT) where a session QUERY_TAG would
            # override any tag they set on the user or account.
            "application": "nivaria",
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

    @property
    def uses_privatelink(self) -> bool:
        """Whether the account identifier routes through PrivateLink, which
        puts Snowsight on its own private host."""
        return ".privatelink" in self.account.casefold()


@dataclass(frozen=True)
class AccountNames:
    """The session's organization and account name, as Snowflake reports
    them -- whatever form the identifier in `ConnectParams` was typed in."""

    organization: str
    account: str


class Connector(Protocol):
    """The one call the pool makes into snowflake.connector.

    Injected so the whole pool is testable without the driver or a network.
    """

    def __call__(self, **kwargs: Any) -> Any: ...


def _real_connector(**kwargs: Any) -> Any:
    # The driver, not this package: imports are absolute.
    import snowflake.connector

    from nivaria.snowflake import credential_cache

    # Must happen before connect() ever touches the driver's token cache --
    # see credential_cache.py for why Windows' default backend cannot hold an
    # OAuth token in the first place.
    credential_cache.install()
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
        #: Held while a replacement connection logs in. See `_open_one`.
        self._login_lock = threading.Lock()
        self._login_failures = 0
        self._login_error = ""
        #: Read once by `connect_all`; None until then, or if Snowflake would
        #: not say.
        self.account_names: AccountNames | None = None

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    @property
    def account_name(self) -> str:
        """The account's name for display, without any host or routing part.

        What Snowflake reports, because the identifier varies with how it was
        typed and whether it routes through PrivateLink. Without that answer,
        the identifier up to its first dot is still short and recognisable,
        though it may keep an organization prefix.

        For display only: connecting, the cache file and Snowsight links use
        the real values, so `ACCOUNT_LABEL_ENV` changes nothing but this name.
        """
        label = os.environ.get(ACCOUNT_LABEL_ENV)
        if label:
            return label
        if self.account_names is not None:
            return self.account_names.account
        return self.params.account.partition(".")[0]

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
            journal.connection(f"login {index + 1} of {self.size}, may prompt")
            try:
                connection = self._connector(**self.params.to_connect_kwargs())
            # The driver raises many unrelated exception types; the caller
            # only needs "connecting failed, here is why".
            except Exception as error:
                raise ConnectionError_(_login_failure_text(error)) from error
            self._all.append(connection)
            self._free.put(connection)
        self._adopt_resolved_session()
        self._read_account_names()

    def _read_account_names(self) -> None:
        """Ask the first connection which organization and account it is in.

        The login response that `_adopt_resolved_session` reads has no such
        field, so this is one query -- here, in the connect worker, so a
        Snowsight link later is a plain attribute read. Not repeated by
        `grow()` or a role switch: neither changes the account.
        """
        if not self._all:
            return
        statement = queries.current_account_names()
        journal.connection("reading the organization and account name")
        try:
            cursor = self._all[0].cursor()
            try:
                sql_log.log_statement(cursor, statement)
                cursor.execute(statement)
                row = cursor.fetchone()
            finally:
                cursor.close()
        # Swallowed, not raised: the names feed a convenience link and a label
        # with a fallback, and a session that connected fine is worth more than
        # either. The driver raises many unrelated types, as in connect_all.
        except Exception:  # noqa: BLE001
            return
        if row and row[0] and row[1]:
            self.account_names = AccountNames(str(row[0]), str(row[1]))

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
            journal.connection(f"login {self.size + 1} of {target}, may prompt")
            try:
                connection = self._connector(**self.params.to_connect_kwargs())
            except Exception as error:
                raise ConnectionError_(_login_failure_text(error)) from error
            self._all.append(connection)
            self._free.put(connection)
            self.size += 1

    @contextlib.contextmanager
    def acquire(self, *, allow_reconnect: bool = True) -> Iterator[Any]:
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
        connection = self._checkout(allow_reconnect=allow_reconnect)
        with self._lock:
            self._in_use += 1
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

    def _checkout(self, *, allow_reconnect: bool) -> Any:
        connection = self._acquire_free(allow_reconnect=allow_reconnect)
        if not self._is_dead(connection):
            return connection
        if allow_reconnect:
            return self._replace(connection)
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

    def _acquire_free(self, *, allow_reconnect: bool) -> Any:
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

        Asked again while waiting, because the answer can change: the
        connection being waited for can be given up (see `_replace`), and
        nothing would then ever arrive on `_free`.
        """
        while True:
            with self._lock:
                live = self.size
                short = live < self._target_size
            if not allow_reconnect and live == 0:
                raise ConnectionError_("connection pool is empty")
            if allow_reconnect and short:
                try:
                    return self._free.get_nowait()
                except queue.Empty:
                    connection = self._open_one()
                    with self._lock:
                        self.size += 1
                    return connection
            with contextlib.suppress(queue.Empty):
                return self._free.get(timeout=_WAIT_RECHECK_SECONDS)

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
        """Open a replacement, one at a time like `connect_all`.

        Sessions die together, so several workers get here at once. After a
        login that worked, the token cache spares the rest a prompt. After one
        that failed, those already waiting on it share its failure rather than
        each opening a window that nobody just answered.
        """
        failures = self._login_failures
        with self._login_lock:
            if self._login_failures != failures:
                journal.connection("login not tried: the one just before failed")
                raise ConnectionError_(self._login_error)
            journal.connection("login to replace a lost connection, may prompt")
            try:
                connection = self._connector(**self.params.to_connect_kwargs())
            # The driver raises many unrelated types, as in connect_all.
            except Exception as error:
                self._login_failures += 1
                self._login_error = _login_failure_text(error)
                raise ConnectionError_(self._login_error) from error
        with self._lock:
            self._all.append(connection)
        return connection

    def _replace(self, connection: Any) -> Any:
        """A new connection for a bad one, or the slot given up if that fails.

        `size` counts what exists. Left as it was after a failed login, the
        pool would read as full, and every later checkout would wait for a
        connection nobody will ever hand back, even once logins work again.
        Short, the next interactive or visible checkout tries the login.
        """
        self._detach(connection)
        try:
            return self._open_one()
        except ConnectionError_:
            with self._lock:
                self.size -= 1
            raise

    def _drop(self, connection: Any) -> None:
        """Detach a bad connection and shrink `size` to match -- no replacement."""
        journal.connection(
            "a dead connection is dropped: background work never logs in"
        )
        self._detach(connection)
        with self._lock:
            self.size -= 1

    def _discard(self, connection: Any, *, allow_reconnect: bool) -> None:
        """A connection that just failed mid-use is never handed out again.

        Mirrors the checkout-time policy: replaced in place (and queued for
        the next caller) when reconnecting is allowed, otherwise dropped and
        the pool shrinks -- repaired later by an interactive/visible caller.
        """
        if allow_reconnect:
            self._free.put(self._replace(connection))
        else:
            self._drop(connection)

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
            journal.connection(f"{statement} on {expected} connections")
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
                        connection = self._replace(connection)
                    held.append(connection)
                for connection in held:
                    cursor = connection.cursor()
                    try:
                        sql_log.log_statement(cursor, statement)
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
        """Close every idle connection; leave one a worker still has
        checked out alone.

        Closing a connection out from under an in-flight `cursor.execute()`
        races the driver's own session teardown (they share one underlying
        HTTP session) and was observed to hang the whole app on quit --
        and, via the identical `stop(); close()` sequence, on a fast
        reconnect too -- while a background fetch was still running a
        query. Workers cannot be interrupted (see docs/DESIGN.md), so a
        still-checked-out connection is simply abandoned here rather than
        raced: the process exiting (or, on reconnect, garbage collection)
        reclaims it for free, and nothing here waits on a straggling query.
        """
        self._closed = True
        idle: list[Any] = []
        while True:
            try:
                idle.append(self._free.get_nowait())
            except queue.Empty:
                break
        for connection in idle:
            with contextlib.suppress(Exception):
                connection.close()
        self._all.clear()
