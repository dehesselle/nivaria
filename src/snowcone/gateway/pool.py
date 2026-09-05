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
from dataclasses import dataclass, field
from typing import Any, Protocol

from snowcone.errors import SnowconeError

EXTERNAL_BROWSER = "externalbrowser"
USERNAME_PASSWORD_MFA = "username_password_mfa"

AUTHENTICATORS = (EXTERNAL_BROWSER, USERNAME_PASSWORD_MFA)


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
        self.size = size
        self._connector = connector or _real_connector
        self._on_usage_change = on_usage_change
        self._free: queue.LifoQueue[Any] = queue.LifoQueue()
        self._all: list[Any] = []
        self._in_use = 0
        self._lock = threading.Lock()
        self._closed = False

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    def connect_all(self, progress: Callable[[int, int], None] | None = None) -> None:
        """Open every connection, one at a time, reporting (opened, total).

        Called from a worker thread; `progress` is invoked from that thread, so
        a UI caller must marshal it back to the event loop itself.
        """
        for index in range(self.size):
            try:
                connection = self._connector(**self.params.to_connect_kwargs())
            # The driver raises many unrelated exception types; the caller
            # only needs "connecting failed, here is why".
            except Exception as error:
                raise ConnectionError_(str(error)) from error
            self._all.append(connection)
            self._free.put(connection)
            if progress is not None:
                progress(index + 1, self.size)
        self._notify_usage()

    def grow(
        self, additional: int, progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Open `additional` more connections on top of what is already open.

        Same one-at-a-time rule as `connect_all`, for the same reason: this is
        the path taken when the user agrees to more than the single connection
        opened up front, and those prompts must not all land at once either.
        `size` grows as each connection lands, so a caller reading it mid-grow
        (the pool gauge, say) sees the true count rather than the eventual one.
        """
        if additional < 1:
            raise ValueError("additional must be at least 1")
        target = self.size + additional
        for _ in range(additional):
            try:
                connection = self._connector(**self.params.to_connect_kwargs())
            except Exception as error:
                raise ConnectionError_(str(error)) from error
            self._all.append(connection)
            self._free.put(connection)
            self.size += 1
            if progress is not None:
                progress(self.size, target)
        self._notify_usage()

    @contextlib.contextmanager
    def acquire(self, timeout: float | None = None) -> Iterator[Any]:
        """Borrow a connection for the duration of the block.

        Blocks while every connection is busy. A connection found closed is
        replaced rather than handed out -- that reconnect may prompt, which is
        why it is reported through the usage gauge like any other activity.
        """
        if self._closed:
            raise ConnectionError_("connection pool is closed")
        connection = self._free.get(timeout=timeout)
        with self._lock:
            self._in_use += 1
        self._notify_usage()
        try:
            yield self._ensure_open(connection)
        finally:
            with self._lock:
                self._in_use -= 1
            self._free.put(connection)
            self._notify_usage()

    def _ensure_open(self, connection: Any) -> Any:
        is_closed = getattr(connection, "is_closed", None)
        if is_closed is None or not is_closed():
            return connection
        replacement = self._connector(**self.params.to_connect_kwargs())
        with self._lock:
            for index, existing in enumerate(self._all):
                if existing is connection:
                    self._all[index] = replacement
                    break
        return replacement

    def close(self) -> None:
        self._closed = True
        for connection in self._all:
            with contextlib.suppress(Exception):
                connection.close()
        self._all.clear()

    def _notify_usage(self) -> None:
        if self._on_usage_change is not None:
            self._on_usage_change(self.in_use, self.size)
