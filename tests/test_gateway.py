# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import logging
from typing import Any

import pytest

from nivaria.gateway.gateway import PoolGateway
from nivaria.gateway.pool import ConnectionError_, ConnectionPool, ConnectParams
from nivaria.models import ObjectType
from tests.fakes import CancelledError, MissingObjectError


class RecordingCursor:
    def __init__(self, owner: RecordingConnection) -> None:
        self._owner = owner
        self.description: list[tuple[str, ...]] = []
        self._rows: list[tuple[Any, ...]] = []
        self.closed = False

    def execute(
        self,
        statement: str,
        params: tuple[Any, ...] | None = None,
        *,
        timeout: int | None = None,
    ) -> None:
        self._owner.attempts += 1
        self._owner.timeouts.append(timeout)
        if self._owner.fail_times > 0:
            self._owner.fail_times -= 1
            raise self._owner.error
        self._owner.executed.append((statement, params))
        self.description = [
            (name, self._owner.type_codes.get(name)) for name in self._owner.columns
        ]
        self._rows = list(self._owner.rows)

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        # Drains, like a real cursor: the gateway reads in batches so it can
        # abandon a runaway result, and a fake that kept returning the same
        # rows forever would make that loop look broken.
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch

    def close(self) -> None:
        self.closed = True
        self._owner.closed_cursors += 1

    def _preprocess_pyformat_query(
        self, statement: str, params: tuple[Any, ...] | None = None
    ) -> str:
        # Stands in for the real connector's bind resolution -- good enough
        # fidelity for a test to assert on, not a claim this matches
        # Snowflake's own pyformat handling exactly.
        return statement % params if params else statement


class RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.columns: list[str] = ["name"]
        self.rows: list[tuple[Any, ...]] = [("ACME",)]
        #: `cursor.description` type code per column, as the driver numbers
        #: them; a column missing here reports None.
        self.type_codes: dict[str, int] = {}
        self.closed_cursors = 0
        #: How many times a query still needs to fail before it succeeds, and
        #: how many times one was actually attempted -- together they let a
        #: test prove there was exactly one retry, not a loop.
        self.fail_times = 0
        self.attempts = 0
        #: The `timeout` each attempt was executed with.
        self.timeouts: list[int | None] = []
        #: What each of those failures raises.
        self.error: Exception = RuntimeError("session expired")

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
    # Forget the pool's own connect-time query, so every count a test reads
    # is the gateway's alone.
    connection.executed.clear()
    connection.timeouts.clear()
    connection.attempts = 0
    connection.closed_cursors = 0
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
    gateway.show_objects(ObjectType.TABLE, "MYDB", "PUBLIC", after="ORDERS", limit=10)
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


def test_a_missing_object_is_not_retried() -> None:
    """A dropped schema is just as dropped on a fresh connection, so a retry
    would only double the round trip."""
    gateway, connection, _ = make_gateway()
    connection.error = MissingObjectError("Schema 'MYDB.GONE' does not exist")
    connection.fail_times = 5
    with pytest.raises(MissingObjectError):
        gateway.show_grants_on_schema("MYDB", "GONE")
    assert connection.attempts == 1


def test_a_missing_object_keeps_its_healthy_connection() -> None:
    """The session that reported "does not exist" is fine; replacing it would
    open a new one, which can mean an MFA prompt."""
    connection = RecordingConnection()
    connects: list[dict[str, Any]] = []

    def connector(**kwargs: Any) -> RecordingConnection:
        connects.append(kwargs)
        return connection

    pool = ConnectionPool(ConnectParams(account="a", user="u"), 1, connector=connector)
    pool.connect_all()
    gateway = PoolGateway(pool)
    connection.error = MissingObjectError("Schema 'MYDB.GONE' does not exist")
    connection.fail_times = 1
    with pytest.raises(MissingObjectError):
        gateway.show_grants_on_schema("MYDB", "GONE")
    assert len(connects) == 1
    assert pool.size == 1
    assert pool.in_use == 0
    assert gateway.show_databases() == [{"name": "ACME"}]
    assert len(connects) == 1


def test_only_the_data_preview_is_executed_with_a_timeout() -> None:
    """The driver cancels a statement past its timeout server-side. Only the
    preview needs one: a view's LIMIT does not bound the work behind it."""
    gateway, connection, _ = make_gateway()
    gateway.show_databases()
    gateway.select_data_preview("MYDB", "PUBLIC", "V", 100, 15)
    assert connection.timeouts == [None, 15]


def test_a_data_preview_drops_the_drivers_json_indentation() -> None:
    """The driver hands VARIANT/OBJECT/ARRAY/GEOGRAPHY/GEOMETRY over as
    indented JSON text, so a one-line Data cell would show mostly spaces. A
    VARCHAR that only looks like JSON is the user's text and stays as it is."""
    gateway, connection, _ = make_gateway()
    indented = '{\n  "a": 1,\n  "b": [\n    true,\n    "x y"\n  ]\n}'
    connection.columns = ["V", "A", "G", "S"]
    connection.type_codes = {"V": 5, "A": 10, "G": 14, "S": 2}
    connection.rows = [(indented, "[\n  1,\n  undefined\n]", None, indented)]
    rows = gateway.select_data_preview("MYDB", "PUBLIC", "T", 100, 15)
    assert rows == [
        {
            "V": '{"a":1,"b":[true,"x y"]}',
            "A": "[1,undefined]",
            "G": None,
            "S": indented,
        }
    ]


def test_only_the_data_preview_compacts_json() -> None:
    gateway, connection, _ = make_gateway()
    connection.columns = ["name"]
    connection.type_codes = {"name": 5}
    connection.rows = [("[\n  1\n]",)]
    assert gateway.show_databases() == [{"name": "[\n  1\n]"}]


def test_a_cancelled_query_is_not_retried() -> None:
    """A statement that ran out its timeout would run it out again, so a
    retry would only double the wait."""
    gateway, connection, _ = make_gateway()
    connection.error = CancelledError("SQL execution canceled")
    connection.fail_times = 5
    with pytest.raises(CancelledError):
        gateway.select_data_preview("MYDB", "PUBLIC", "V", 100, 15)
    assert connection.attempts == 1


def test_a_cancelled_query_keeps_its_healthy_connection() -> None:
    """Cancelling one query leaves its session fine; replacing it would open
    a new one, which can mean an MFA prompt."""
    connection = RecordingConnection()
    connects: list[dict[str, Any]] = []

    def connector(**kwargs: Any) -> RecordingConnection:
        connects.append(kwargs)
        return connection

    pool = ConnectionPool(ConnectParams(account="a", user="u"), 1, connector=connector)
    pool.connect_all()
    gateway = PoolGateway(pool)
    connection.error = CancelledError("SQL execution canceled")
    connection.fail_times = 1
    with pytest.raises(CancelledError):
        gateway.select_data_preview("MYDB", "PUBLIC", "V", 100, 15)
    assert len(connects) == 1
    assert pool.size == 1
    assert pool.in_use == 0
    assert gateway.show_databases() == [{"name": "ACME"}]
    assert len(connects) == 1


# -------------------------------------------------------------- sql logging


def test_every_statement_is_logged_at_debug_under_the_sql_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, connection, _ = make_gateway()
    connection.columns = ["name"]
    connection.rows = [("ACME",)]
    with caplog.at_level(logging.DEBUG, logger="sql"):
        gateway.show_databases()
    records = [record for record in caplog.records if record.name == "sql"]
    assert len(records) == 1
    assert "SHOW DATABASES" in records[0].message


def test_the_statement_is_logged_even_though_execute_then_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Logging happens before execute, not after -- the entire point is
    seeing the last statement even when it never returns successfully."""
    gateway, connection, _ = make_gateway()
    connection.fail_times = 1
    with caplog.at_level(logging.DEBUG, logger="sql"):
        gateway.show_databases()
    records = [record for record in caplog.records if record.name == "sql"]
    # One line per real attempt against Snowflake, including the failed one.
    assert len(records) == 2


def test_nothing_is_logged_when_the_sql_source_is_not_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, _, _ = make_gateway()
    gateway.show_databases()
    assert [record for record in caplog.records if record.name == "sql"] == []


def test_show_warehouses_sends_the_unpaged_statement() -> None:
    gateway, connection, _ = make_gateway()
    connection.columns = ["name", "comment"]
    connection.rows = [("MYWH", "")]
    assert gateway.show_warehouses() == [{"name": "MYWH", "comment": ""}]
    statement, params = connection.executed[-1]
    assert statement == "SHOW WAREHOUSES"
    assert params is None


def test_show_roles_sends_the_unpaged_statement() -> None:
    gateway, connection, _ = make_gateway()
    connection.columns = ["name", "comment"]
    connection.rows = [("SYSADMIN", "")]
    assert gateway.show_roles() == [{"name": "SYSADMIN", "comment": ""}]
    statement, params = connection.executed[-1]
    assert statement == "SHOW ROLES"
    assert params is None


def test_table_stats_binds_schema_and_table_type_as_parameters() -> None:
    """The first gateway call that actually populates `params` -- unlike the
    SHOW builders, TABLE_SCHEMA/TABLE_TYPE are bound, not interpolated."""
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "TABLE_CATALOG",
        "TABLE_SCHEMA",
        "TABLE_NAME",
        "LAST_DDL",
        "LAST_ALTERED",
    ]
    connection.rows = [("MYDB", "PUBLIC", "ORDERS", "2026-01-01", "2026-01-02")]
    rows = gateway.table_stats("MYDB", "PUBLIC")
    statement, params = connection.executed[-1]
    assert 'FROM "MYDB".INFORMATION_SCHEMA.TABLES' in statement
    assert params == ("PUBLIC", "BASE TABLE")
    assert rows[0]["TABLE_NAME"] == "ORDERS"


def test_view_stats_binds_schema_and_view_table_type_as_parameters() -> None:
    """Same statement as table_stats, just bound to TABLE_TYPE = 'VIEW'."""
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "TABLE_CATALOG",
        "TABLE_SCHEMA",
        "TABLE_NAME",
        "LAST_DDL",
        "LAST_ALTERED",
    ]
    connection.rows = [("MYDB", "PUBLIC", "ORDERS_V", "2026-01-01", "2026-01-02")]
    rows = gateway.view_stats("MYDB", "PUBLIC")
    statement, params = connection.executed[-1]
    assert 'FROM "MYDB".INFORMATION_SCHEMA.TABLES' in statement
    assert params == ("PUBLIC", "VIEW")
    assert rows[0]["TABLE_NAME"] == "ORDERS_V"


def test_materialized_view_stats_sends_the_unpaged_show_statement() -> None:
    """Metadata layer like every other SHOW builder -- no bound params, and
    no LIMIT/FROM paging tail issued, same as table_stats being unpaged."""
    gateway, connection, _ = make_gateway()
    connection.columns = ["database_name", "schema_name", "name", "rows", "bytes"]
    connection.rows = [("MYDB", "PUBLIC", "TOTALS_MV", 42, 8192)]
    rows = gateway.materialized_view_stats("MYDB", "PUBLIC")
    statement, params = connection.executed[-1]
    assert statement.startswith('SHOW MATERIALIZED VIEWS IN SCHEMA "MYDB"."PUBLIC"')
    assert params is None
    assert rows[0]["name"] == "TOTALS_MV"


def test_pipe_activity_binds_database_and_schema_as_parameters() -> None:
    """Unlike table_stats, the database is not interpolated into the FROM
    target here -- ACCOUNT_USAGE.COPY_HISTORY is a fixed name -- so both
    filter values are bound."""
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "PIPE_CATALOG_NAME",
        "PIPE_SCHEMA_NAME",
        "PIPE_NAME",
        "FILE_NAME",
        "FILE_SIZE",
        "ROW_COUNT",
        "STATUS",
        "LAST_LOAD_TIME",
        "TABLE_CATALOG_NAME",
        "TABLE_SCHEMA_NAME",
        "TABLE_NAME",
    ]
    connection.rows = [
        (
            "MYDB",
            "PUBLIC",
            "MY_PIPE",
            "data/orders_1.csv.gz",
            1024,
            42,
            "Loaded",
            "2026-01-02T03:04:05+00:00",
            "MYDB",
            "PUBLIC",
            "ORDERS",
        )
    ]
    rows = gateway.pipe_activity("MYDB", "PUBLIC", 90)
    statement, params = connection.executed[-1]
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY" in statement
    assert "DATEADD('day', -90," in statement
    assert params == ("MYDB", "PUBLIC")
    assert rows[0]["PIPE_NAME"] == "MY_PIPE"


def test_pipe_activity_history_binds_database_schema_and_pipe_as_parameters() -> None:
    """The per-pipe backfill: same source as pipe_activity, plus a third
    bound PIPE_NAME param instead of a QUALIFY reduction."""
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "PIPE_CATALOG_NAME",
        "PIPE_SCHEMA_NAME",
        "PIPE_NAME",
        "FILE_NAME",
        "FILE_SIZE",
        "ROW_COUNT",
        "STATUS",
        "LAST_LOAD_TIME",
        "TABLE_CATALOG_NAME",
        "TABLE_SCHEMA_NAME",
        "TABLE_NAME",
    ]
    connection.rows = [
        (
            "MYDB",
            "PUBLIC",
            "MY_PIPE",
            "data/orders_1.csv.gz",
            1024,
            42,
            "Loaded",
            "2026-01-02T03:04:05+00:00",
            "MYDB",
            "PUBLIC",
            "ORDERS",
        )
    ]
    rows = gateway.pipe_activity_history("MYDB", "PUBLIC", "MY_PIPE", 90, 5000)
    statement, params = connection.executed[-1]
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY" in statement
    assert "QUALIFY" not in statement
    assert params == ("MYDB", "PUBLIC", "MY_PIPE")
    assert rows[0]["PIPE_NAME"] == "MY_PIPE"


def test_pipe_history_binds_pipe_name_and_qualifies_the_target_table() -> None:
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "FILE_NAME",
        "FILE_SIZE",
        "ROW_COUNT",
        "STATUS",
        "LAST_LOAD_TIME",
        "PIPE_NAME",
    ]
    connection.rows = [
        (
            "orders_9.csv.gz",
            2048,
            100,
            "Loaded",
            "2026-03-01T08:00:00+00:00",
            "LOAD_ORDERS",
        )
    ]
    rows = gateway.pipe_history("MYDB", "PUBLIC", "ORDERS", "LOAD_ORDERS", 5000)
    statement, params = connection.executed[-1]
    assert 'FROM TABLE("MYDB".INFORMATION_SCHEMA.COPY_HISTORY(' in statement
    assert "TABLE_NAME => 'MYDB.PUBLIC.ORDERS'" in statement
    assert params == ("LOAD_ORDERS",)
    assert rows[0]["FILE_NAME"] == "orders_9.csv.gz"


def test_task_activity_binds_database_and_schema_as_parameters() -> None:
    """Same shape as pipe_activity, against ACCOUNT_USAGE.TASK_HISTORY."""
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "NAME",
        "DATABASE_NAME",
        "SCHEMA_NAME",
        "QUERY_ID",
        "SCHEDULED_TIME",
        "QUERY_START_TIME",
        "COMPLETED_TIME",
        "STATE",
        "ERROR_CODE",
        "ERROR_MESSAGE",
    ]
    connection.rows = [
        (
            "REFRESH_ORDERS",
            "MYDB",
            "PUBLIC",
            "01ab-query-id",
            "2026-01-02T03:04:05+00:00",
            "2026-01-02T03:04:06+00:00",
            "2026-01-02T03:04:10+00:00",
            "SUCCEEDED",
            None,
            None,
        )
    ]
    rows = gateway.task_activity("MYDB", "PUBLIC", 90)
    statement, params = connection.executed[-1]
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY" in statement
    assert "DATEADD('day', -90," in statement
    assert params == ("MYDB", "PUBLIC")
    assert rows[0]["NAME"] == "REFRESH_ORDERS"


def test_task_activity_history_binds_database_schema_and_task_as_parameters() -> None:
    """The per-task backfill: same source as task_activity, plus a third
    bound NAME param instead of a QUALIFY reduction."""
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "NAME",
        "DATABASE_NAME",
        "SCHEMA_NAME",
        "QUERY_ID",
        "SCHEDULED_TIME",
        "QUERY_START_TIME",
        "COMPLETED_TIME",
        "STATE",
        "ERROR_CODE",
        "ERROR_MESSAGE",
    ]
    connection.rows = [
        (
            "REFRESH_ORDERS",
            "MYDB",
            "PUBLIC",
            "01ab-query-id",
            "2026-01-02T03:04:05+00:00",
            "2026-01-02T03:04:06+00:00",
            "2026-01-02T03:04:10+00:00",
            "SUCCEEDED",
            None,
            None,
        )
    ]
    rows = gateway.task_activity_history("MYDB", "PUBLIC", "REFRESH_ORDERS", 90, 5000)
    statement, params = connection.executed[-1]
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY" in statement
    assert "QUALIFY" not in statement
    assert params == ("MYDB", "PUBLIC", "REFRESH_ORDERS")
    assert rows[0]["NAME"] == "REFRESH_ORDERS"


def test_task_history_calls_the_table_function_with_no_bound_params() -> None:
    """Unlike pipe_history, DATABASE_NAME/SCHEMA_NAME/TASK_NAME are direct
    named arguments, so there is nothing left to bind. The function name
    itself is still database-qualified, same as pipe_history qualifies
    COPY_HISTORY -- see queries.task_history's docstring."""
    gateway, connection, _ = make_gateway()
    connection.columns = [
        "NAME",
        "QUERY_ID",
        "SCHEDULED_TIME",
        "QUERY_START_TIME",
        "COMPLETED_TIME",
        "STATE",
        "ERROR_CODE",
        "ERROR_MESSAGE",
    ]
    connection.rows = [
        (
            "REFRESH_ORDERS",
            "01ab-query-id",
            "2026-03-01T08:00:00+00:00",
            "2026-03-01T08:00:01+00:00",
            "2026-03-01T08:00:05+00:00",
            "SUCCEEDED",
            None,
            None,
        )
    ]
    rows = gateway.task_history("MYDB", "PUBLIC", "REFRESH_ORDERS", 5000)
    statement, params = connection.executed[-1]
    assert 'FROM TABLE("MYDB".INFORMATION_SCHEMA.TASK_HISTORY(' in statement
    assert "TASK_NAME => 'REFRESH_ORDERS'" in statement
    assert params is None
    assert rows[0]["NAME"] == "REFRESH_ORDERS"


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


def test_a_result_past_the_row_ceiling_is_abandoned() -> None:
    """The bound that applies to every statement, not just the ones somebody
    remembered to cap. Snowflake has returned more than nivaria asked for
    often enough that the per-statement limits are an expectation rather than
    a guarantee, and materialising an unbounded result pins a pool connection
    for as long as it takes."""
    from nivaria.gateway.gateway import ResultTooLargeError
    from nivaria.settings import ROW_CEILING, config

    config.set(ROW_CEILING, 100)
    gateway, connection, _ = make_gateway()
    connection.rows = [(f"T{index}",) for index in range(500)]
    with pytest.raises(ResultTooLargeError) as excinfo:
        gateway.show_databases()
    assert "100" in str(excinfo.value)


def test_a_result_at_the_row_ceiling_is_still_returned() -> None:
    """The ceiling is a limit, not a threshold to stay under: a result of
    exactly N rows is a complete, legitimate answer."""
    from nivaria.settings import ROW_CEILING, config

    config.set(ROW_CEILING, 100)
    gateway, connection, _ = make_gateway()
    connection.rows = [(f"T{index}",) for index in range(100)]
    assert len(gateway.show_databases()) == 100


def test_an_over_ceiling_result_is_not_retried() -> None:
    """_fetch retries any failure once, which is right for a session the
    server dropped and wrong here: the statement will return just as much the
    second time, so the retry only pays for the damage twice."""
    from nivaria.gateway.gateway import ResultTooLargeError
    from nivaria.settings import ROW_CEILING, config

    config.set(ROW_CEILING, 10)
    gateway, connection, _ = make_gateway()
    connection.rows = [(f"T{index}",) for index in range(50)]
    with pytest.raises(ResultTooLargeError):
        gateway.show_databases()
    assert connection.attempts == 1


def test_the_row_ceiling_can_be_switched_off() -> None:
    """0 disables it, per the setting's own description -- someone who knows
    what they are doing gets to decide the ceiling is in their way."""
    from nivaria.settings import ROW_CEILING, config

    config.set(ROW_CEILING, 0)
    gateway, connection, _ = make_gateway()
    connection.rows = [(f"T{index}",) for index in range(5000)]
    assert len(gateway.show_databases()) == 5000
