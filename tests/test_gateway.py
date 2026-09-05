from __future__ import annotations

from typing import Any

import pytest

from snowcone.gateway.gateway import PoolGateway
from snowcone.gateway.pool import ConnectionError_, ConnectionPool, ConnectParams
from snowcone.models import ObjectKind


class RecordingCursor:
    def __init__(self, owner: RecordingConnection) -> None:
        self._owner = owner
        self.description: list[tuple[str, ...]] = []
        self._rows: list[tuple[Any, ...]] = []
        self.closed = False

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        self._owner.attempts += 1
        if self._owner.fail_times > 0:
            self._owner.fail_times -= 1
            raise RuntimeError("session expired")
        self._owner.executed.append((statement, params))
        self.description = [(name,) for name in self._owner.columns]
        self._rows = list(self._owner.rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        self.closed = True
        self._owner.closed_cursors += 1


class RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.columns: list[str] = ["name"]
        self.rows: list[tuple[Any, ...]] = [("ACME",)]
        self.closed_cursors = 0
        #: How many times a query still needs to fail before it succeeds, and
        #: how many times one was actually attempted -- together they let a
        #: test prove there was exactly one retry, not a loop.
        self.fail_times = 0
        self.attempts = 0

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)

    def is_closed(self) -> bool:
        return False


def make_gateway() -> tuple[PoolGateway, RecordingConnection, ConnectionPool]:
    connection = RecordingConnection()
    pool = ConnectionPool(
        ConnectParams(account="a", user="u"), 1, connector=lambda **_: connection
    )
    pool.connect_all()
    return PoolGateway(pool), connection, pool


def test_rows_are_keyed_by_snowflakes_own_column_names() -> None:
    """Nothing is renamed on the way in: the cache tables downstream are a
    faithful mirror, so there is no private vocabulary to translate."""
    gateway, connection, _ = make_gateway()
    connection.columns = ["created_on", "name", "database_name"]
    connection.rows = [("2026-01-01", "PUBLIC", "MYDB")]
    assert gateway.show_schemas("MYDB") == [
        {"created_on": "2026-01-01", "name": "PUBLIC", "database_name": "MYDB"}
    ]


def test_show_objects_sends_the_statement_the_builder_produced() -> None:
    gateway, connection, _ = make_gateway()
    gateway.show_objects(ObjectKind.TABLE, "MYDB", "PUBLIC", after="ORDERS", limit=10)
    statement, params = connection.executed[-1]
    assert statement == 'SHOW TABLES IN SCHEMA "MYDB"."PUBLIC" LIMIT 10 FROM \'ORDERS\''
    assert params is None


def test_show_columns_targets_the_fully_qualified_table() -> None:
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "table_name",
        "schema_name",
        "database_name",
        "column_name",
        "data_type",
    ]
    connection.rows = [("ORDERS", "PUBLIC", "MYDB", "ID", '{"type":"NUMBER"}')]
    gateway.show_columns("MYDB", "PUBLIC", "ORDERS")
    statement, params = connection.executed[-1]
    assert statement == 'SHOW COLUMNS IN TABLE "MYDB"."PUBLIC"."ORDERS"'
    assert params is None


def test_show_columns_is_mapped_before_it_leaves_the_gateway() -> None:
    """The cache only ever sees the INFORMATION_SCHEMA shape, whichever
    statement produced the rows."""
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "table_name",
        "schema_name",
        "database_name",
        "column_name",
        "data_type",
    ]
    connection.rows = [
        ("ORDERS", "PUBLIC", "MYDB", "ID", '{"type":"NUMBER","precision":38}')
    ]
    rows = gateway.show_columns("MYDB", "PUBLIC", "ORDERS")
    assert rows[0]["COLUMN_NAME"] == "ID"
    assert rows[0]["DATA_TYPE"] == "NUMBER"
    assert rows[0]["ORDINAL_POSITION"] == 1


def test_the_cursor_is_closed_even_though_the_connection_is_reused() -> None:
    """Connections are persistent for the whole session, so a cursor leaked
    per query would accumulate for as long as the app runs."""
    gateway, connection, _ = make_gateway()
    gateway.show_databases()
    gateway.show_databases()
    assert connection.closed_cursors == 2


def test_the_connection_is_returned_to_the_pool_after_each_call() -> None:
    """One call == one page == one checkout, so no query can monopolise a
    connection while a user waits to expand a node."""
    gateway, _, pool = make_gateway()
    gateway.show_databases()
    assert pool.in_use == 0


def test_an_empty_result_is_an_empty_list_not_an_error() -> None:
    gateway, connection, _ = make_gateway()
    connection.rows = []
    assert gateway.show_databases() == []


# --------------------------------------------------------- retry on failure


def test_a_query_that_fails_once_is_retried_and_succeeds() -> None:
    """A connection can look open at checkout and still fail the instant it
    is used -- the gap a checkout-time is_closed() check cannot see. One
    retry against a freshly-acquired connection covers it."""
    gateway, connection, _ = make_gateway()
    connection.fail_times = 1
    assert gateway.show_databases() == [{"name": "ACME"}]
    assert connection.attempts == 2


def test_a_query_that_keeps_failing_surfaces_the_real_error() -> None:
    """Exactly one retry, not a loop -- a genuine, non-staleness failure must
    still surface rather than be retried forever."""
    gateway, connection, _ = make_gateway()
    connection.fail_times = 5
    with pytest.raises(RuntimeError):
        gateway.show_databases()
    assert connection.attempts == 2


def test_background_priority_retries_but_never_reconnects_unattended() -> None:
    """The retry itself must respect allow_reconnect: a background-priority
    caller whose only connection just failed must not have it replaced by a
    freshly opened, possibly-interactive session."""
    gateway, connection, _ = make_gateway()
    connection.fail_times = 1
    with pytest.raises(ConnectionError_):
        gateway.show_databases(allow_reconnect=False)
    # One real attempt, then the retry's own acquire fails fast -- no second
    # query attempt against a reconnected session.
    assert connection.attempts == 1
