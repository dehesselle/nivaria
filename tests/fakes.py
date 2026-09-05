"""Offline stand-ins for everything that would otherwise touch Snowflake.

No test may import snowflake.connector: the driver opens sockets, reads
credential caches from the real user profile, and on some auth modes tries to
open a browser. These fakes let the cache, the scheduler and the whole UI be
driven end to end with none of that.
"""

from __future__ import annotations

import threading
from typing import Any

from snowcone.gateway import queries
from snowcone.gateway.pool import ConnectionPool, ConnectParams
from snowcone.models import ObjectKind

Row = dict[str, Any]


class FakeCursor:
    def __init__(self, owner: FakeConnection) -> None:
        self._owner = owner

    def execute(self, statement: str) -> None:
        self._owner.executed.append(statement)

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        #: Every statement run via cursor(), so a test can prove exactly which
        #: connections a pool-wide change like set_role reached.
        self.executed: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


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
        warehouses: list[str] | None = None,
        roles: list[str] | None = None,
    ) -> None:
        self.databases = databases or []
        self.schemas = schemas or {}
        self.objects = objects or {}
        self.columns = columns or {}
        self.warehouses = warehouses or []
        self.roles = roles or []
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
        self,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(("show_databases", (after, limit)))
        return [
            {"name": name, "comment": ""}
            for name in self._page(self.databases, after, limit)
        ]

    def show_schemas(
        self,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
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
        allow_reconnect: bool = True,
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

    def show_objects_in_database(
        self,
        kind: ObjectKind,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(("show_objects_in_database", (kind, database, after, limit)))
        everything: list[tuple[str, str]] = []
        for (db, schema, object_kind), names in self.objects.items():
            if db != database or object_kind != kind:
                continue
            everything.extend((name, schema) for name in names)
        everything.sort()
        if after is not None:
            everything = [pair for pair in everything if pair[0] > after]
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
            for name, schema in everything[:limit]
        ]

    def show_columns(
        self,
        database: str,
        schema: str,
        table: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        """Returns raw SHOW COLUMNS shape, so the real mapper is exercised."""
        self.calls.append(("show_columns", (database, schema, table)))
        raw = [
            {
                "table_name": table,
                "schema_name": schema,
                "database_name": database,
                "column_name": name,
                "data_type": '{"type":"TEXT","length":16777216,"nullable":true}',
                "null?": "Y",
                "default": None,
                "kind": "COLUMN",
                "expression": None,
                "comment": "",
                "autoincrement": "",
            }
            for name in self.columns.get((database, schema, table), [])
        ]
        return queries.map_show_columns(raw)

    def _raw_columns(
        self, database: str, schema: str, table: str
    ) -> list[dict[str, Any]]:
        return [
            {
                "table_name": table,
                "schema_name": schema,
                "database_name": database,
                "column_name": name,
                "data_type": '{"type":"TEXT","length":16777216,"nullable":true}',
                "null?": "Y",
                "default": None,
                "kind": "COLUMN",
                "expression": None,
                "comment": "",
                "autoincrement": "",
            }
            for name in self.columns.get((database, schema, table), [])
        ]

    def show_columns_in_database(
        self,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        """Every column of every table in `database`, raw-shaped like the fake
        `show_columns` above, so `map_show_columns` is exercised for real."""
        self.calls.append(("show_columns_in_database", (database, after, limit)))
        raw: list[dict[str, Any]] = []
        for db, schema, table in sorted(self.columns):
            if db != database:
                continue
            raw.extend(self._raw_columns(db, schema, table))
        return queries.map_show_columns(raw[:limit])

    def show_columns_in_schema(
        self,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(("show_columns_in_schema", (database, schema, after, limit)))
        raw: list[dict[str, Any]] = []
        for db, sch, table in sorted(self.columns):
            if db != database or sch != schema:
                continue
            raw.extend(self._raw_columns(db, sch, table))
        return queries.map_show_columns(raw[:limit])

    def show_warehouses(self, *, allow_reconnect: bool = True) -> list[Row]:
        self.calls.append(("show_warehouses", ()))
        return [{"name": name, "comment": ""} for name in sorted(self.warehouses)]

    def show_roles(self, *, allow_reconnect: bool = True) -> list[Row]:
        self.calls.append(("show_roles", ()))
        return [{"name": name, "comment": ""} for name in sorted(self.roles)]
