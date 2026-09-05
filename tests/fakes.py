"""Offline stand-ins for everything that would otherwise touch Snowflake.

No test may import snowflake.connector: the driver opens sockets, reads
credential caches from the real user profile, and on some auth modes tries to
open a browser. These fakes let the cache, the scheduler and the whole UI be
driven end to end with none of that.
"""

from __future__ import annotations

import threading
from typing import Any

from snowcone.gateway.pool import ConnectionPool, ConnectParams
from snowcone.models import ObjectKind

Row = dict[str, Any]


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class FakeConnector:
    """Stands in for snowflake.connector.connect.

    Records how many times it was called so a test can prove the pool opened
    exactly N connections and never silently opened an extra one.
    """

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_after = fail_after
        self._lock = threading.Lock()

    def __call__(self, **kwargs: Any) -> FakeConnection:
        with self._lock:
            if self.fail_after is not None and len(self.calls) >= self.fail_after:
                raise RuntimeError("simulated connection failure")
            self.calls.append(kwargs)
        return FakeConnection()


def make_pool(size: int = 2, **kwargs: Any) -> tuple[ConnectionPool, FakeConnector]:
    connector = FakeConnector()
    pool = ConnectionPool(
        ConnectParams(account="acct", user="user"),
        size,
        connector=connector,
        **kwargs,
    )
    return pool, connector


class FakeGateway:
    """A gateway backed by in-memory dicts, with real keyset paging.

    Paging is implemented for real rather than stubbed, so tests of the fetch
    scheduler exercise the same multi-page code path production will.
    """

    def __init__(
        self,
        databases: list[str] | None = None,
        schemas: dict[str, list[str]] | None = None,
        objects: dict[tuple[str, str, ObjectKind], list[str]] | None = None,
        columns: dict[tuple[str, str, str], list[str]] | None = None,
    ) -> None:
        self.databases = databases or []
        self.schemas = schemas or {}
        self.objects = objects or {}
        self.columns = columns or {}
        #: Every call, so a test can assert *what was fetched*, not merely
        #: what ended up rendered.
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    @staticmethod
    def _page(names: list[str], after: str | None, limit: int) -> list[str]:
        ordered = sorted(names)
        if after is not None:
            ordered = [name for name in ordered if name > after]
        return ordered[:limit]

    def show_databases(
        self, *, after: str | None = None, limit: int = 1000
    ) -> list[Row]:
        self.calls.append(("show_databases", (after, limit)))
        return [
            {"name": name, "comment": ""}
            for name in self._page(self.databases, after, limit)
        ]

    def show_schemas(
        self, database: str, *, after: str | None = None, limit: int = 1000
    ) -> list[Row]:
        self.calls.append(("show_schemas", (database, after, limit)))
        names = self._page(self.schemas.get(database, []), after, limit)
        return [
            {"name": name, "database_name": database, "comment": ""} for name in names
        ]

    def show_objects(
        self,
        kind: ObjectKind,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
    ) -> list[Row]:
        self.calls.append(("show_objects", (kind, database, schema, after, limit)))
        names = self._page(self.objects.get((database, schema, kind), []), after, limit)
        return [
            {
                "name": name,
                "database_name": database,
                "schema_name": schema,
                "kind": kind.value,
                "comment": "",
                "rows": 0,
                "bytes": 0,
            }
            for name in names
        ]

    def information_schema_columns(
        self, database: str, schema: str, table: str | None = None
    ) -> list[Row]:
        self.calls.append(("information_schema_columns", (database, schema, table)))
        rows: list[Row] = []
        for (db, sch, tbl), names in sorted(self.columns.items()):
            if db != database or sch != schema:
                continue
            if table is not None and tbl != table:
                continue
            for position, name in enumerate(names, start=1):
                rows.append(
                    {
                        "TABLE_CATALOG": db,
                        "TABLE_SCHEMA": sch,
                        "TABLE_NAME": tbl,
                        "COLUMN_NAME": name,
                        "ORDINAL_POSITION": position,
                        "DATA_TYPE": "TEXT",
                        "IS_NULLABLE": "YES",
                        "COLUMN_DEFAULT": None,
                        "CHARACTER_MAXIMUM_LENGTH": None,
                        "NUMERIC_PRECISION": None,
                        "NUMERIC_SCALE": None,
                        "COMMENT": "",
                    }
                )
        return rows
