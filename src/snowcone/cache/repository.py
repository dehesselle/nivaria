"""The typed read/write API over the cache.

Readers run on the Textual event loop and must stay in the microsecond range;
writers run on fetch worker threads.

The write side is a **pass protocol**, and it is what makes a chunked refresh
safe to show while it is still running::

    generation = repo.begin_pass(scope)                    # claims the scope
    repo.write_page(scope, generation, rows, next_cursor)  # repeat per page
    repo.finish_pass(scope, generation)                    # sweep + complete

Every row a pass writes is stamped with that pass's generation. Rows are only
deleted at `finish_pass`, by sweeping whatever the pass did *not* rewrite. So
an object that still exists is never briefly absent, and the UI never flashes
an empty list on the way to a full one -- while a deleted object still
disappears, exactly one pass later.

A write carrying a stale generation is ignored, which is how a superseded job
that is already mid-flight cannot corrupt the scope that replaced it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from snowcone.cache.db import Database
from snowcone.models import (
    CacheState,
    Column,
    ObjectKind,
    ObjectRef,
    SchemaRef,
    Scope,
    ScopeKind,
    SFObject,
    schemas_scope,
)
from snowcone.models import Database as DatabaseRow
from snowcone.models import Role as RoleRow
from snowcone.models import Schema as SchemaRow
from snowcone.models import Warehouse as WarehouseRow


@dataclass(frozen=True)
class TableSpec:
    """Which mirror table backs a scope, and which of its columns we store."""

    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    order_by: tuple[str, ...]


_DATABASES = TableSpec(
    name="show_databases",
    columns=(
        "name",
        "created_on",
        "kind",
        "owner",
        "owner_role_type",
        "comment",
        "options",
        "origin",
        "retention_time",
    ),
    key=("name",),
    order_by=("name",),
)

_SCHEMAS = TableSpec(
    name="show_schemas",
    columns=(
        "database_name",
        "name",
        "created_on",
        "owner",
        "owner_role_type",
        "comment",
        "options",
        "retention_time",
    ),
    key=("database_name", "name"),
    order_by=("name",),
)

_OBJECT_TABLES: dict[ObjectKind, TableSpec] = {
    ObjectKind.TABLE: TableSpec(
        name="show_tables",
        columns=(
            "database_name",
            "schema_name",
            "name",
            "created_on",
            "kind",
            "comment",
            "cluster_by",
            "rows",
            "bytes",
            "owner",
            "owner_role_type",
            "retention_time",
            "automatic_clustering",
            "change_tracking",
            "search_optimization",
            "is_external",
            "is_event",
            "is_hybrid",
            "is_iceberg",
            "is_dynamic",
        ),
        key=("database_name", "schema_name", "name"),
        order_by=("name",),
    ),
    ObjectKind.VIEW: TableSpec(
        name="show_views",
        columns=(
            "database_name",
            "schema_name",
            "name",
            "created_on",
            "comment",
            "owner",
            "owner_role_type",
            "text",
            "is_secure",
            "is_materialized",
            "change_tracking",
        ),
        key=("database_name", "schema_name", "name"),
        order_by=("name",),
    ),
    ObjectKind.PIPE: TableSpec(
        name="show_pipes",
        columns=(
            "database_name",
            "schema_name",
            "name",
            "created_on",
            "definition",
            "owner",
            "owner_role_type",
            "notification_channel",
            "comment",
            "pattern",
            "integration",
            "error_integration",
            "invalid_reason",
            "kind",
            "is_snowflake_managed",
        ),
        key=("database_name", "schema_name", "name"),
        order_by=("name",),
    ),
    ObjectKind.STAGE: TableSpec(
        name="show_stages",
        columns=(
            "database_name",
            "schema_name",
            "name",
            "created_on",
            "url",
            "type",
            "owner",
            "owner_role_type",
            "comment",
            "region",
            "cloud",
            "storage_integration",
            "endpoint",
            "has_credentials",
            "directory_enabled",
        ),
        key=("database_name", "schema_name", "name"),
        order_by=("name",),
    ),
    ObjectKind.TASK: TableSpec(
        name="show_tasks",
        columns=(
            "database_name",
            "schema_name",
            "name",
            "created_on",
            "definition",
            "owner",
            "owner_role_type",
            "comment",
            "warehouse",
            "schedule",
            "state",
            "predecessors",
            "condition",
            "error_integration",
            "last_committed_on",
            "last_suspended_on",
        ),
        key=("database_name", "schema_name", "name"),
        order_by=("name",),
    ),
}

_WAREHOUSES = TableSpec(
    name="show_warehouses",
    columns=("name", "comment"),
    key=("name",),
    order_by=("name",),
)

_ROLES = TableSpec(
    name="show_roles",
    columns=("name", "comment"),
    key=("name",),
    order_by=("name",),
)

_COLUMNS = TableSpec(
    name="information_schema_columns",
    columns=(
        "TABLE_CATALOG",
        "TABLE_SCHEMA",
        "TABLE_NAME",
        "COLUMN_NAME",
        "ORDINAL_POSITION",
        "DATA_TYPE",
        "IS_NULLABLE",
        "COLUMN_DEFAULT",
        "CHARACTER_MAXIMUM_LENGTH",
        "NUMERIC_PRECISION",
        "NUMERIC_SCALE",
        "COMMENT",
    ),
    key=("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME"),
    order_by=("TABLE_NAME", "ORDINAL_POSITION"),
)


def spec_for(scope: Scope) -> TableSpec:
    match scope.kind:
        case ScopeKind.DATABASES:
            return _DATABASES
        case ScopeKind.SCHEMAS:
            return _SCHEMAS
        case ScopeKind.OBJECTS:
            assert scope.object_kind is not None
            return _OBJECT_TABLES[scope.object_kind]
        case ScopeKind.COLUMNS:
            return _COLUMNS
        case ScopeKind.WAREHOUSES:
            return _WAREHOUSES
        case ScopeKind.ROLES:
            return _ROLES
        case ScopeKind.DATABASE_PRIME:
            # A prime is a fetch strategy, not a set of rows: it fills other
            # scopes' tables and owns none of its own.
            raise ValueError(f"{scope.kind} has no mirror table")
        case ScopeKind.COLUMNS_PRIME:
            # Same as DATABASE_PRIME: fills other tables' COLUMNS scopes.
            raise ValueError(f"{scope.kind} has no mirror table")


@dataclass(frozen=True)
class Entry:
    """A scope's freshness, independent of its rows."""

    scope_key: str
    state: CacheState
    generation: int
    fetched_at: float | None
    row_count: int
    error: str | None

    def age(self, now: float | None = None) -> float | None:
        if self.fetched_at is None:
            return None
        return max(0.0, (now if now is not None else time.time()) - self.fetched_at)


MISSING = Entry(
    scope_key="",
    state=CacheState.EMPTY,
    generation=0,
    fetched_at=None,
    row_count=0,
    error=None,
)


@dataclass(frozen=True)
class Cached[T]:
    """Rows plus how old they are -- the only shape the UI reads."""

    rows: list[T]
    entry: Entry

    @property
    def state(self) -> CacheState:
        return self.entry.state

    @property
    def fetched_at(self) -> float | None:
        return self.entry.fetched_at

    def age(self, now: float | None = None) -> float | None:
        return self.entry.age(now)

    @property
    def is_empty(self) -> bool:
        return not self.rows


def _quote(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


class Repository:
    def __init__(self, database: Database) -> None:
        self._db = database

    # ---------------------------------------------------------------- reads

    def entry(self, scope: Scope) -> Entry:
        with self._db.connect() as connection:
            return self._entry(connection, scope)

    @staticmethod
    def _entry(connection: sqlite3.Connection, scope: Scope) -> Entry:
        row = connection.execute(
            "SELECT * FROM cache_entry WHERE scope = ?", (scope.key,)
        ).fetchone()
        if row is None:
            return MISSING
        return Entry(
            scope_key=row["scope"],
            state=CacheState(row["state"]),
            generation=row["generation"],
            fetched_at=row["fetched_at"],
            row_count=row["row_count"],
            error=row["error"],
        )

    def _select(
        self, connection: sqlite3.Connection, scope: Scope
    ) -> list[sqlite3.Row]:
        spec = spec_for(scope)
        order = ", ".join(_quote(column) for column in spec.order_by)
        # Table and column names come from the module-level specs above, never
        # from user input, so the interpolation here cannot carry a payload.
        statement = f"SELECT * FROM {spec.name} WHERE _scope = ? ORDER BY {order}"
        return connection.execute(statement, (scope.key,)).fetchall()

    def raw_rows(self, scope: Scope) -> list[dict[str, Any]]:
        """Every mirrored column of a scope, as stored."""
        with self._db.connect() as connection:
            return [dict(row) for row in self._select(connection, scope)]

    def _cached(self, scope: Scope, build: Callable[[sqlite3.Row], Any]) -> Cached[Any]:
        with self._db.connect() as connection:
            entry = self._entry(connection, scope)
            rows = self._select(connection, scope)
        return Cached(rows=[build(row) for row in rows], entry=entry)

    def databases(self, scope: Scope) -> Cached[DatabaseRow]:
        return self._cached(
            scope,
            lambda row: DatabaseRow(name=row["name"], comment=row["comment"] or ""),
        )

    def warehouses(self, scope: Scope) -> Cached[WarehouseRow]:
        return self._cached(
            scope,
            lambda row: WarehouseRow(name=row["name"], comment=row["comment"] or ""),
        )

    def roles(self, scope: Scope) -> Cached[RoleRow]:
        return self._cached(
            scope,
            lambda row: RoleRow(name=row["name"], comment=row["comment"] or ""),
        )

    def schemas(self, scope: Scope) -> Cached[SchemaRow]:
        return self._cached(
            scope,
            lambda row: SchemaRow(
                database_name=row["database_name"],
                name=row["name"],
                comment=row["comment"] or "",
            ),
        )

    def objects(self, scope: Scope) -> Cached[SFObject]:
        assert scope.object_kind is not None
        kind = scope.object_kind
        mirrored = set(_OBJECT_TABLES[kind].columns)

        def build(row: sqlite3.Row) -> SFObject:
            return SFObject(
                database_name=row["database_name"],
                schema_name=row["schema_name"],
                name=row["name"],
                kind=kind,
                comment=row["comment"] or "",
                rows=row["rows"] if "rows" in mirrored else None,
                bytes=row["bytes"] if "bytes" in mirrored else None,
            )

        return self._cached(scope, build)

    def columns(self, scope: Scope) -> Cached[Column]:
        return self._cached(
            scope,
            lambda row: Column(
                table_catalog=row["TABLE_CATALOG"],
                table_schema=row["TABLE_SCHEMA"],
                table_name=row["TABLE_NAME"],
                column_name=row["COLUMN_NAME"],
                ordinal_position=row["ORDINAL_POSITION"] or 0,
                data_type=row["DATA_TYPE"] or "",
                is_nullable=(row["IS_NULLABLE"] or "").upper() == "YES",
                default=row["COLUMN_DEFAULT"] or "",
                comment=row["COMMENT"] or "",
            ),
        )

    def object_detail(self, ref: ObjectRef) -> dict[str, Any] | None:
        """One object's mirrored row, addressed by primary key.

        A detail view wants one row; reading a whole schema's list and
        filtering in Python would mean building several hundred objects to
        show one.
        """
        spec = _OBJECT_TABLES[ref.kind]
        statement = (
            f"SELECT * FROM {spec.name} "
            "WHERE database_name = ? AND schema_name = ? AND name = ?"
        )
        with self._db.connect() as connection:
            row = connection.execute(
                statement, (ref.database, ref.schema, ref.name)
            ).fetchone()
        return dict(row) if row is not None else None

    def search_corpus(self, database: str) -> list[tuple[str, ObjectRef]]:
        """Every searchable object in a database as (text, ref) pairs.

        The text is `SCHEMA.NAME`, so typing "sales.ord" narrows by schema and
        object together. Ranking happens in Python: sqlite can match
        substrings but not subsequences, and "stgord" finding
        STG_CUSTOMER_ORDERS is the whole point.
        """
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT database_name, schema_name, name, kind FROM search_object "
                "WHERE database_name = ? ORDER BY schema_name, name",
                (database,),
            ).fetchall()
        corpus: list[tuple[str, ObjectRef]] = []
        for row in rows:
            try:
                kind = ObjectKind(row["kind"])
            except ValueError:
                continue
            corpus.append(
                (
                    f"{row['schema_name']}.{row['name']}",
                    ObjectRef(
                        kind=kind,
                        database=row["database_name"],
                        schema=row["schema_name"],
                        name=row["name"],
                    ),
                )
            )
        return corpus

    def search_column_corpus(self, database: str) -> list[tuple[ObjectRef, str]]:
        """Every cached column in a database, paired with its owning object.

        Joined back to `search_object` rather than read off
        `information_schema_columns` alone, so a column only surfaces if its
        owning table/view is itself still known there -- a stale leftover
        column row from a since-dropped table cannot spuriously appear. Only
        TABLE/VIEW ever have columns, but the join excludes other kinds
        regardless of whether that ever actually matters.
        """
        with self._db.connect() as connection:
            rows = connection.execute(
                """
                SELECT o.schema_name, o.name, o.kind, c.COLUMN_NAME
                FROM information_schema_columns c
                JOIN search_object o
                  ON o.database_name = c.TABLE_CATALOG
                 AND o.schema_name  = c.TABLE_SCHEMA
                 AND o.name         = c.TABLE_NAME
                WHERE c.TABLE_CATALOG = ? AND o.kind IN ('TABLE', 'VIEW')
                ORDER BY o.schema_name, o.name, c.ORDINAL_POSITION
                """,
                (database,),
            ).fetchall()
        # One ObjectRef per column here would mean one allocation per column
        # rather than per table -- 15-20x more than necessary at a typical
        # column-per-table ratio, and it showed: ~30ms of a ~73ms call on a
        # 30k-row corpus was this constructor alone. The ORDER BY above
        # already groups every column under its owner, so consecutive rows
        # sharing a key can just reuse the previous ObjectRef instead.
        corpus: list[tuple[ObjectRef, str]] = []
        current_key: tuple[str, str] | None = None
        current_ref: ObjectRef | None = None
        for row in rows:
            key = (row["schema_name"], row["name"])
            if key != current_key:
                current_key = key
                try:
                    kind = ObjectKind(row["kind"])
                except ValueError:
                    current_ref = None
                else:
                    current_ref = ObjectRef(
                        kind=kind,
                        database=database,
                        schema=row["schema_name"],
                        name=row["name"],
                    )
            if current_ref is not None:
                corpus.append((current_ref, row["COLUMN_NAME"]))
        return corpus

    def search_schema_corpus(self, database: str) -> list[SchemaRef]:
        """Every cached schema in a database, for the search modal.

        Schemas are already flat and singly-sourced (`show_schemas`), unlike
        objects and columns, so this is a thin wrapper over `schemas()`
        rather than its own query -- kept anyway, so the modal reads three
        parallel `search_*_corpus` methods instead of building one of its
        three corpora by hand.
        """
        return [
            SchemaRef(database=database, name=row.name)
            for row in self.schemas(schemas_scope(database)).rows
        ]

    def child_count(self, scope: Scope) -> int:
        """The count behind a collapsed tree label, e.g. `Schemas (40)`."""
        spec = spec_for(scope)
        statement = f"SELECT COUNT(*) AS n FROM {spec.name} WHERE _scope = ?"
        with self._db.connect() as connection:
            return int(connection.execute(statement, (scope.key,)).fetchone()["n"])

    def stale_scopes(self, ttl: float, among: Iterable[Scope]) -> list[Scope]:
        """Which of `among` are due a background refresh.

        Restricted to the scopes the caller passes in -- the UI only offers
        those it is actually showing, so nothing nobody is looking at is
        refreshed. That is what keeps a refresh targeted rather than a sweep
        of the whole account.
        """
        if ttl <= 0:
            return []
        now = time.time()
        stale: list[Scope] = []
        with self._db.connect() as connection:
            for scope in among:
                entry = self._entry(connection, scope)
                if entry.state is CacheState.EMPTY:
                    stale.append(scope)
                    continue
                age = entry.age(now)
                if age is None or age >= ttl:
                    stale.append(scope)
        return stale

    # --------------------------------------------------------------- writes

    def begin_pass(self, scope: Scope) -> int:
        """Claim the scope for a new refresh pass and return its generation.

        `fetched_at` is deliberately carried over rather than cleared: until
        this pass finishes, the age the status bar shows is still the age of
        the rows actually on screen.
        """
        with self._db.connect() as connection:
            entry = self._entry(connection, scope)
            generation = entry.generation + 1
            connection.execute(
                """
                INSERT INTO cache_entry
                    (scope, state, generation, fetched_at,
                     next_cursor, row_count, error)
                VALUES (?, ?, ?, ?, NULL, 0, NULL)
                ON CONFLICT(scope) DO UPDATE SET
                    state = excluded.state,
                    generation = excluded.generation,
                    next_cursor = NULL,
                    row_count = 0,
                    error = NULL
                """,
                (
                    scope.key,
                    CacheState.PARTIAL.value,
                    generation,
                    entry.fetched_at,
                ),
            )
        return generation

    def write_page(
        self,
        scope: Scope,
        generation: int,
        rows: Sequence[dict[str, Any]],
        next_cursor: str | None,
    ) -> bool:
        """Upsert one page. Returns False if this pass has been superseded.

        The generation check is what fences a job that was already mid-flight
        when the user switched away: thread workers cannot be interrupted, so
        their writes have to be rejected on arrival instead.
        """
        spec = spec_for(scope)
        now = time.time()
        with self._db.connect() as connection:
            if self._entry(connection, scope).generation != generation:
                return False
            if rows:
                self._upsert(connection, spec, scope, generation, rows, now)
            connection.execute(
                "UPDATE cache_entry SET next_cursor = ?, row_count = row_count + ? "
                "WHERE scope = ?",
                (next_cursor, len(rows), scope.key),
            )
        return True

    @staticmethod
    def _upsert(
        connection: sqlite3.Connection,
        spec: TableSpec,
        scope: Scope,
        generation: int,
        rows: Sequence[dict[str, Any]],
        now: float,
    ) -> None:
        columns = (*spec.columns, "_scope", "_generation", "_fetched_at")
        placeholders = ", ".join("?" for _ in columns)
        quoted = ", ".join(_quote(column) for column in columns)
        conflict = ", ".join(_quote(column) for column in spec.key)
        updates = ", ".join(
            f"{_quote(column)} = excluded.{_quote(column)}"
            for column in columns
            if column not in spec.key
        )
        statement = (
            f"INSERT INTO {spec.name} ({quoted}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
        )
        # Snowflake may return columns we do not mirror; row.get drops those
        # rather than failing, which is what lets a mirror cover only the
        # useful subset of a SHOW result.
        connection.executemany(
            statement,
            [
                [row.get(column) for column in spec.columns]
                + [scope.key, generation, now]
                for row in rows
            ],
        )

    def finish_pass(self, scope: Scope, generation: int) -> bool:
        """Sweep rows this pass did not rewrite and mark the scope complete."""
        spec = spec_for(scope)
        now = time.time()
        sweep = f"DELETE FROM {spec.name} WHERE _scope = ? AND _generation < ?"
        count_rows = f"SELECT COUNT(*) AS n FROM {spec.name} WHERE _scope = ?"
        with self._db.connect() as connection:
            if self._entry(connection, scope).generation != generation:
                return False
            connection.execute(sweep, (scope.key, generation))
            count = connection.execute(count_rows, (scope.key,)).fetchone()["n"]
            connection.execute(
                """
                UPDATE cache_entry
                   SET state = ?, fetched_at = ?, next_cursor = NULL,
                       row_count = ?, error = NULL
                 WHERE scope = ?
                """,
                (CacheState.COMPLETE.value, now, count, scope.key),
            )
        return True

    def replace_grouped(
        self, grouped: Mapping[Scope, Sequence[dict[str, Any]]]
    ) -> None:
        """Run a complete pass for each scope from one already-fetched result.

        Used by the database-wide prime: one SHOW returns every schema's
        objects, so each schema's scope is begun, written and swept here
        rather than each costing its own round trip.

        Scopes mapped to an empty list still get a full pass. That is the
        point of passing them: a schema with no tables has to read as
        "fetched, none" rather than "never fetched", or the tree would keep
        asking for it and the node would never settle.
        """
        for scope, rows in grouped.items():
            generation = self.begin_pass(scope)
            self.write_page(scope, generation, rows, None)
            self.finish_pass(scope, generation)

    def fail_pass(self, scope: Scope, generation: int, error: str) -> bool:
        """Record a failure without sweeping.

        Stale rows stay visible: showing yesterday's tables beats showing an
        empty schema because the warehouse happened to be unreachable.
        """
        with self._db.connect() as connection:
            if self._entry(connection, scope).generation != generation:
                return False
            connection.execute(
                "UPDATE cache_entry SET state = ?, error = ? WHERE scope = ?",
                (CacheState.ERROR.value, error, scope.key),
            )
        return True
