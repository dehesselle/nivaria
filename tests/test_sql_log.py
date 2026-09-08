"""`sql_log.log_statement` in isolation, with no pool or gateway involved.

`tests/test_gateway.py` and `tests/test_pool.py` prove the real choke points
call this; these tests prove the wrapper itself behaves correctly regardless
of caller.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from snowcone.gateway import sql_log


class RaisingCursor:
    """A cursor whose private resolution methods raise if ever touched --
    proves the isEnabledFor guard skips them entirely rather than merely
    discarding a formatted record afterwards."""

    def _preprocess_pyformat_query(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("must not be called when the sql source is not at debug")


class ResolvingCursor:
    def __init__(self) -> None:
        self.executed = False

    def _preprocess_pyformat_query(
        self, statement: str, params: tuple[Any, ...] | None = None
    ) -> str:
        return statement % params if params else statement

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed = True


class BrokenPreprocessCursor:
    """Simulates a future connector version where the private method has been
    renamed or removed -- logging must degrade, never break the query."""

    def _preprocess_pyformat_query(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("private API no longer exists")

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        pass


def test_the_resolved_statement_is_logged_under_the_sql_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="sql"):
        sql_log.log_statement(ResolvingCursor(), "SELECT * FROM T WHERE ID = %s", (42,))
    records = [record for record in caplog.records if record.name == "sql"]
    assert len(records) == 1
    assert records[0].message == "SELECT * FROM T WHERE ID = 42"


def test_the_statement_is_logged_before_execute_is_called(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entire point: a statement that hangs or raises must still have
    been logged, since it already logs before execute runs at all."""

    class FailingCursor(ResolvingCursor):
        def execute(
            self, statement: str, params: tuple[Any, ...] | None = None
        ) -> None:
            raise RuntimeError("connection dropped mid-query")

    cursor = FailingCursor()
    with caplog.at_level(logging.DEBUG, logger="sql"):
        sql_log.log_statement(cursor, "SHOW DATABASES")
        with pytest.raises(RuntimeError):
            cursor.execute("SHOW DATABASES")
    records = [record for record in caplog.records if record.name == "sql"]
    assert len(records) == 1


def test_nothing_is_logged_and_resolution_is_skipped_below_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cursor = RaisingCursor()
    sql_log.log_statement(cursor, "SHOW DATABASES")  # must not raise
    assert [record for record in caplog.records if record.name == "sql"] == []


def test_a_broken_resolution_falls_back_to_the_raw_statement(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cursor = BrokenPreprocessCursor()
    with caplog.at_level(logging.DEBUG, logger="sql"):
        sql_log.log_statement(cursor, "SELECT %s", (1,))
        cursor.execute("SELECT %s", (1,))  # must still run fine
    records = [record for record in caplog.records if record.name == "sql"]
    assert len(records) == 1
    assert records[0].message == "SELECT %s"


def test_a_long_statement_is_logged_in_full_not_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The connector's own `_format_query_for_log` cuts off at 80 characters
    by default -- fine for its own terse internal debug stream, but this
    feature exists specifically so the whole statement is visible, so it
    must not inherit that cutoff."""
    long_statement = "SELECT " + ", ".join(f"COL_{i}" for i in range(30)) + " FROM T"
    assert len(long_statement) > 80
    with caplog.at_level(logging.DEBUG, logger="sql"):
        sql_log.log_statement(ResolvingCursor(), long_statement)
    [record] = [record for record in caplog.records if record.name == "sql"]
    assert record.message == long_statement
