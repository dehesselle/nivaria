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
from snowcone.models import ObjectType

Row = dict[str, Any]


class MissingObjectError(Exception):
    """Stands in for the driver's ProgrammingError for a dropped object:
    Snowflake reports it as errno 2003, "... does not exist or not
    authorized", and that number is all `is_missing_object` reads."""

    errno = 2003


class CancelledError(Exception):
    """Stands in for the driver's error for a statement its timeout
    cancelled: Snowflake reports it as errno 604, "SQL execution canceled",
    and that number is all `is_cancelled` reads."""

    errno = 604


class FakeCursor:
    def __init__(self, owner: FakeConnection) -> None:
        self._owner = owner

    def execute(self, statement: str) -> None:
        self._owner.executed.append(statement)

    def close(self) -> None:
        pass

    def _preprocess_pyformat_query(
        self, statement: str, params: tuple[Any, ...] | None = None
    ) -> str:
        # Stands in for the real connector's bind resolution -- good enough
        # fidelity for a test to assert on, not a claim this matches
        # Snowflake's own pyformat handling exactly.
        return statement % params if params else statement


class FakeConnection:
    def __init__(
        self, *, role: str | None = None, warehouse: str | None = None
    ) -> None:
        self.closed = False
        #: Every statement run via cursor(), so a test can prove exactly which
        #: connections a pool-wide change like set_role reached.
        self.executed: list[str] = []
        #: What the real driver would report from the login response's
        #: sessionInfo, for tests of ConnectionPool._adopt_resolved_session.
        self.role = role
        self.warehouse = warehouse

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

    def __init__(
        self,
        *,
        fail_after: int | None = None,
        resolved_role: str | None = None,
        resolved_warehouse: str | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_after = fail_after
        #: What every connection this produces reports as its role/warehouse,
        #: standing in for what Snowflake's login response would resolve.
        self.resolved_role = resolved_role
        self.resolved_warehouse = resolved_warehouse
        self._lock = threading.Lock()

    def __call__(self, **kwargs: Any) -> FakeConnection:
        with self._lock:
            if self.fail_after is not None and len(self.calls) >= self.fail_after:
                raise RuntimeError("simulated connection failure")
            self.calls.append(kwargs)
        return FakeConnection(
            role=self.resolved_role, warehouse=self.resolved_warehouse
        )


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
        objects: dict[tuple[str, str, ObjectType], list[str]] | None = None,
        columns: dict[tuple[str, str, str], list[str]] | None = None,
        warehouses: list[str] | None = None,
        roles: list[str] | None = None,
        table_stats: dict[tuple[str, str], list[Row]] | None = None,
        view_stats: dict[tuple[str, str], list[Row]] | None = None,
        materialized_view_stats: dict[tuple[str, str], list[Row]] | None = None,
        pipe_activity: dict[tuple[str, str], list[Row]] | None = None,
        pipe_activity_history: dict[tuple[str, str, str], list[Row]] | None = None,
        pipe_history: dict[tuple[str, str, str, str], list[Row]] | None = None,
        task_activity: dict[tuple[str, str], list[Row]] | None = None,
        task_activity_history: dict[tuple[str, str, str], list[Row]] | None = None,
        task_history: dict[tuple[str, str, str], list[Row]] | None = None,
        grants_schema: dict[tuple[str, str], list[Row]] | None = None,
        grants_database: dict[str, list[Row]] | None = None,
        grants_object: dict[tuple[ObjectType, str, str, str], list[Row]] | None = None,
        tag_references_schema: dict[tuple[str, str], list[Row]] | None = None,
        tag_references_database: dict[str, list[Row]] | None = None,
        tag_references_all_columns: dict[tuple[str, str, str], list[Row]] | None = None,
        data_preview: dict[tuple[str, str, str], list[Row]] | None = None,
    ) -> None:
        self.databases = databases or []
        self.schemas = schemas or {}
        self.objects = objects or {}
        self.columns = columns or {}
        self.warehouses = warehouses or []
        self.roles = roles or []
        # Named differently from the table_stats()/pipe_activity()/
        # pipe_history() methods below -- an instance attribute of the same
        # name would shadow them.
        self._table_stats = table_stats or {}
        self._view_stats = view_stats or {}
        self._materialized_view_stats = materialized_view_stats or {}
        self._pipe_activity = pipe_activity or {}
        #: Keyed by (database, schema, pipe_name).
        self._pipe_activity_history = pipe_activity_history or {}
        #: Keyed by (target_catalog, target_schema, target_table, pipe_name).
        self._pipe_history = pipe_history or {}
        self._task_activity = task_activity or {}
        #: Keyed by (database, schema, task_name).
        self._task_activity_history = task_activity_history or {}
        #: Keyed by (database, schema, task_name) directly -- unlike
        #: _pipe_history, task_history needs no resolved target table.
        self._task_history = task_history or {}
        self._grants_schema = grants_schema or {}
        self._grants_database = grants_database or {}
        #: Keyed by (object_type, database, schema, name).
        self._grants_object = grants_object or {}
        self._tag_references_schema = tag_references_schema or {}
        self._tag_references_database = tag_references_database or {}
        #: Keyed by (database, schema, table).
        self._tag_references_all_columns = tag_references_all_columns or {}
        #: Keyed by (database, schema, table).
        self._data_preview = data_preview or {}
        #: Flip to simulate a warehouse-dependent query failing, e.g. "no
        #: active warehouse" -- table_stats is the first fetch that needs one.
        self.raise_on_table_stats = False
        #: Flip to simulate view_stats' SELECT failing (e.g. no warehouse),
        #: same as raise_on_table_stats above.
        self.raise_on_view_stats = False
        #: Flip to simulate the ACCOUNT_USAGE grant being missing, the other
        #: way pipe_activity's SELECT can fail beyond a missing warehouse.
        self.raise_on_pipe_activity = False
        #: Flip to simulate pipe_activity_history's SELECT failing, same as
        #: raise_on_pipe_activity above.
        self.raise_on_pipe_activity_history = False
        #: Flip to simulate pipe_history's SELECT failing (e.g. no warehouse).
        self.raise_on_pipe_history = False
        #: Flip to simulate the ACCOUNT_USAGE grant being missing, same as
        #: raise_on_pipe_activity above but for task_activity's SELECT.
        self.raise_on_task_activity = False
        #: Flip to simulate task_activity_history's SELECT failing, same as
        #: raise_on_task_activity above.
        self.raise_on_task_activity_history = False
        #: Flip to simulate task_history's SELECT failing (e.g. no warehouse).
        self.raise_on_task_history = False
        #: Flip to simulate tag_references' SELECT failing for lack of an
        #: active warehouse -- show_grants_on_schema/_database need no such
        #: flag, since SHOW GRANTS is metadata-layer like show_warehouses/
        #: show_roles above.
        self.raise_on_tag_references = False
        #: Flip to simulate tag_references_all_columns' SELECT failing, same
        #: as raise_on_tag_references above.
        self.raise_on_tag_references_all_columns = False
        #: Flip to simulate select_data_preview failing for lack of an active
        #: warehouse, same as raise_on_tag_references above.
        self.raise_on_data_preview = False
        #: Object types for which this fake reproduces Snowflake's own
        #: LIMIT/FROM no-op: SHOW returns every row in the schema or
        #: database whatever the caller asked for. Empty by default, so
        #: ordinary tests still exercise real paging; a test that wants to
        #: prove the scheduler survives the misbehaviour opts in. It is a
        #: knob rather than a hard-coded {PIPE, STAGE} because the point of
        #: the guard is that it holds for a type nobody has verified yet.
        self.paging_noop_types: set[ObjectType] = set()
        #: Schemas dropped by `drop_schema`: statements naming one fail the
        #: way Snowflake's do, rather than quietly returning nothing.
        self.dropped_schemas: set[tuple[str, str]] = set()
        #: Every call, so a test can assert *what was fetched*, not merely
        #: what ended up rendered.
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def drop_schema(self, database: str, schema: str) -> None:
        """Drop a schema "elsewhere", as if done in Snowsight."""
        self.schemas[database] = [
            name for name in self.schemas.get(database, []) if name != schema
        ]
        self.dropped_schemas.add((database, schema))

    def _check_schema(self, database: str, schema: str) -> None:
        if (database, schema) in self.dropped_schemas:
            raise MissingObjectError(
                "SQL compilation error:\n"
                f"Schema '{database}.{schema}' does not exist or not authorized."
            )

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
        object_type: ObjectType,
        database: str,
        schema: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(
            ("show_objects", (object_type, database, schema, after, limit))
        )
        self._check_schema(database, schema)
        all_names = self.objects.get((database, schema, object_type), [])
        if object_type in self.paging_noop_types:
            names = sorted(all_names)
        else:
            names = self._page(all_names, after, limit)
        return [
            {
                "name": name,
                "database_name": database,
                "schema_name": schema,
                "kind": object_type.value,
                "comment": "",
                "rows": 0,
                "bytes": 0,
            }
            for name in names
        ]

    def show_objects_in_database(
        self,
        object_type: ObjectType,
        database: str,
        *,
        after: str | None = None,
        limit: int = 1000,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(
            ("show_objects_in_database", (object_type, database, after, limit))
        )
        everything: list[tuple[str, str]] = []
        for (db, schema, row_type), names in self.objects.items():
            if db != database or row_type != object_type:
                continue
            everything.extend((name, schema) for name in names)
        everything.sort()
        if object_type not in self.paging_noop_types:
            if after is not None:
                everything = [pair for pair in everything if pair[0] > after]
            everything = everything[:limit]
        return [
            {
                "name": name,
                "database_name": database,
                "schema_name": schema,
                "kind": object_type.value,
                "comment": "",
                "rows": 0,
                "bytes": 0,
            }
            for name, schema in everything
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
        self._check_schema(database, schema)
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
        self, database: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        """Every column of every table in `database`, raw-shaped like the fake
        `show_columns` above, so `map_show_columns` is exercised for real.

        Returns everything, with nothing to page or cap by, because that is
        what the real statement does -- SHOW COLUMNS ignores LIMIT outright.
        The caller chunks on the response's size instead.
        """
        self.calls.append(("show_columns_in_database", (database,)))
        raw: list[dict[str, Any]] = []
        for db, schema, table in sorted(self.columns):
            if db != database:
                continue
            raw.extend(self._raw_columns(db, schema, table))
        return queries.map_show_columns(raw)

    def show_columns_in_schema(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        self.calls.append(("show_columns_in_schema", (database, schema)))
        raw: list[dict[str, Any]] = []
        for db, sch, table in sorted(self.columns):
            if db != database or sch != schema:
                continue
            raw.extend(self._raw_columns(db, sch, table))
        return queries.map_show_columns(raw)

    def show_warehouses(self, *, allow_reconnect: bool = True) -> list[Row]:
        self.calls.append(("show_warehouses", ()))
        return [{"name": name, "comment": ""} for name in sorted(self.warehouses)]

    def show_roles(self, *, allow_reconnect: bool = True) -> list[Row]:
        self.calls.append(("show_roles", ()))
        return [{"name": name, "comment": ""} for name in sorted(self.roles)]

    def table_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        self.calls.append(("table_stats", (database, schema)))
        if self.raise_on_table_stats:
            raise RuntimeError("No active warehouse selected in the current session")
        return list(self._table_stats.get((database, schema), []))

    def view_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        self.calls.append(("view_stats", (database, schema)))
        if self.raise_on_view_stats:
            raise RuntimeError("No active warehouse selected in the current session")
        return list(self._view_stats.get((database, schema), []))

    def materialized_view_stats(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        self.calls.append(("materialized_view_stats", (database, schema)))
        return list(self._materialized_view_stats.get((database, schema), []))

    def pipe_activity(
        self,
        database: str,
        schema: str,
        fetch_days: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(("pipe_activity", (database, schema, fetch_days)))
        if self.raise_on_pipe_activity:
            raise RuntimeError(
                "SQL access control error: Insufficient privileges to "
                "operate on database 'SNOWFLAKE'"
            )
        return list(self._pipe_activity.get((database, schema), []))

    def pipe_activity_history(
        self,
        database: str,
        schema: str,
        pipe: str,
        fetch_days: int,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        key = (database, schema, pipe)
        self.calls.append(("pipe_activity_history", (*key, fetch_days, max_rows)))
        if self.raise_on_pipe_activity_history:
            raise RuntimeError(
                "SQL access control error: Insufficient privileges to "
                "operate on database 'SNOWFLAKE'"
            )
        return list(self._pipe_activity_history.get(key, []))[:max_rows]

    def pipe_history(
        self,
        target_catalog: str,
        target_schema: str,
        target_table: str,
        pipe_name: str,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        key = (target_catalog, target_schema, target_table, pipe_name)
        self.calls.append(("pipe_history", (*key, max_rows)))
        if self.raise_on_pipe_history:
            raise RuntimeError("No active warehouse selected in the current session")
        return list(self._pipe_history.get(key, []))[:max_rows]

    def task_activity(
        self,
        database: str,
        schema: str,
        fetch_days: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(("task_activity", (database, schema, fetch_days)))
        if self.raise_on_task_activity:
            raise RuntimeError(
                "SQL access control error: Insufficient privileges to "
                "operate on database 'SNOWFLAKE'"
            )
        return list(self._task_activity.get((database, schema), []))

    def task_activity_history(
        self,
        database: str,
        schema: str,
        task: str,
        fetch_days: int,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        key = (database, schema, task)
        self.calls.append(("task_activity_history", (*key, fetch_days, max_rows)))
        if self.raise_on_task_activity_history:
            raise RuntimeError(
                "SQL access control error: Insufficient privileges to "
                "operate on database 'SNOWFLAKE'"
            )
        return list(self._task_activity_history.get(key, []))[:max_rows]

    def task_history(
        self,
        database: str,
        schema: str,
        task: str,
        max_rows: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        key = (database, schema, task)
        self.calls.append(("task_history", (*key, max_rows)))
        if self.raise_on_task_history:
            raise RuntimeError("No active warehouse selected in the current session")
        return list(self._task_history.get(key, []))[:max_rows]

    def show_grants_on_schema(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        self.calls.append(("show_grants_on_schema", (database, schema)))
        self._check_schema(database, schema)
        return list(self._grants_schema.get((database, schema), []))

    def show_grants_on_database(
        self, database: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        self.calls.append(("show_grants_on_database", (database,)))
        return list(self._grants_database.get(database, []))

    def show_grants_on_object(
        self,
        object_type: ObjectType,
        database: str,
        schema: str,
        name: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        key = (object_type, database, schema, name)
        self.calls.append(("show_grants_on_object", key))
        self._check_schema(database, schema)
        return list(self._grants_object.get(key, []))

    def tag_references_schema(
        self, database: str, schema: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        self.calls.append(("tag_references_schema", (database, schema)))
        self._check_schema(database, schema)
        if self.raise_on_tag_references:
            raise RuntimeError("No active warehouse selected in the current session")
        return list(self._tag_references_schema.get((database, schema), []))

    def tag_references_database(
        self, database: str, *, allow_reconnect: bool = True
    ) -> list[Row]:
        self.calls.append(("tag_references_database", (database,)))
        if self.raise_on_tag_references:
            raise RuntimeError("No active warehouse selected in the current session")
        return list(self._tag_references_database.get(database, []))

    def tag_references_all_columns(
        self,
        database: str,
        schema: str,
        table: str,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(("tag_references_all_columns", (database, schema, table)))
        if self.raise_on_tag_references_all_columns:
            raise RuntimeError("No active warehouse selected in the current session")
        return list(self._tag_references_all_columns.get((database, schema, table), []))

    def select_data_preview(
        self,
        database: str,
        schema: str,
        table: str,
        max_rows: int,
        timeout: int,
        *,
        allow_reconnect: bool = True,
    ) -> list[Row]:
        self.calls.append(
            ("select_data_preview", (database, schema, table, max_rows, timeout))
        )
        self._check_schema(database, schema)
        if self.raise_on_data_preview:
            raise RuntimeError("No active warehouse selected in the current session")
        return list(self._data_preview.get((database, schema, table), []))[:max_rows]
