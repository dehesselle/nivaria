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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from snowcone.cache.db import Database
from snowcone.models import CacheState, Column, ObjectKind, Scope, ScopeKind, SFObject
from snowcone.models import Database as DatabaseRow
from snowcone.models import Schema as SchemaRow


@dataclass(frozen=True)
class TableSpec:
    """Which mirror table backs a scope, and which of its columns we store."""

    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    order_by: tuple[str, ...]


_DATABASES = TableSpec(
    name="show_databases",
    columns=("name", "created_on", "kind", "owner", "comment", "retention_time"),
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
        "comment",
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
            "retention_time",
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
            "text",
            "is_secure",
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
            "notification_channel",
            "comment",
            "pattern",
            "integration",
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
            "comment",
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
            "comment",
            "warehouse",
            "schedule",
            "state",
        ),
        key=("database_name", "schema_name", "name"),
        order_by=("name",),
    ),
}

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
                comment=row["COMMENT"] or "",
            ),
        )

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
        now = time.time()
        with self._db.connect() as connection:
            entry = self._entry(connection, scope)
            generation = entry.generation + 1
            connection.execute(
                """
                INSERT INTO cache_entry
                    (scope, state, generation, started_at, fetched_at,
                     next_cursor, row_count, error)
                VALUES (?, ?, ?, ?, ?, NULL, 0, NULL)
                ON CONFLICT(scope) DO UPDATE SET
                    state = excluded.state,
                    generation = excluded.generation,
                    started_at = excluded.started_at,
                    next_cursor = NULL,
                    row_count = 0,
                    error = NULL
                """,
                (
                    scope.key,
                    CacheState.PARTIAL.value,
                    generation,
                    now,
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
