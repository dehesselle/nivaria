# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The typed read/write API over the cache.

Readers run on the Textual event loop and must stay in the microsecond range;
writers run on fetch worker threads.

The write side is a **pass protocol**, and it is what makes a chunked refresh
safe to show while it is still running::

    generation = repo.begin_pass(scope)                    # claims the scope
    repo.write_page(scope, generation, rows, next_cursor)  # repeat per page
    repo.finish_pass(scope, generation)                    # sweep + complete

Every row a pass writes is stamped with that pass's generation. Rows are only
deleted at `finish_pass`, and only those the pass is responsible for: for an
ordinary pass, whatever it did *not* rewrite. So an object that still exists
is never briefly absent, and the UI never flashes an empty list on the way to
a full one -- while a deleted object still disappears, exactly one pass later.
A window pass of a log scope asks only for what is new and deletes only what
that supersedes -- see `plan_window` and `finish_pass`.

A write carrying a stale generation is ignored, which is how a superseded job
that is already mid-flight cannot corrupt the scope that replaced it.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nivaria.cache.db import Database, MemoryDatabase
from nivaria.models import (
    CacheState,
    Column,
    ObjectRef,
    ObjectType,
    SchemaRef,
    Scope,
    ScopeType,
    SFObject,
    due_in,
    pipe_activity_history_scope,
    pipe_activity_scope,
    schemas_scope,
)
from nivaria.models import Database as DatabaseRow
from nivaria.models import Role as RoleRow
from nivaria.models import Schema as SchemaRow
from nivaria.models import Warehouse as WarehouseRow
from nivaria.pipe_definition import copy_target


@dataclass(frozen=True)
class LogShape:
    """How a log scope's rows are ordered in time, for a window pass.

    A log scope mirrors an append-only `ACCOUNT_USAGE` view, whose rows do
    not change once they appear, so a refresh only has to ask for what is
    new -- see `Repository.finish_pass`. The column names are the mirror's,
    and every comparison goes through `julianday()`: the values are ISO
    strings carrying the session's offset, which do not sort as text.
    """

    #: The column the statement's time window filters on. Rows older than
    #: the fetch window by this column are pruned.
    window_time: str
    #: The column that decides which row is newer, for `latest_per` and for
    #: a row cap.
    newest_by: str
    #: For a scope reduced to one row per object (`PIPE_ACTIVITY`,
    #: `TASK_ACTIVITY`), the columns naming that object; None for a scope
    #: holding every row.
    latest_per: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TableSpec:
    """Which mirror table backs a scope, and which of its columns we store."""

    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    order_by: tuple[str, ...]
    #: Set for a log scope, which can be refreshed by a window pass.
    log: LogShape | None = None


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

_OBJECT_TABLES: dict[ObjectType, TableSpec] = {
    ObjectType.TABLE: TableSpec(
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
    ObjectType.VIEW: TableSpec(
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
    ObjectType.PIPE: TableSpec(
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
    ObjectType.STAGE: TableSpec(
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
    ObjectType.TASK: TableSpec(
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
    columns=("name", "comment", "size", "is_default"),
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
        "DATETIME_PRECISION",
        "COMMENT",
        "_desc_type",
    ),
    key=("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME"),
    order_by=("TABLE_NAME", "ORDINAL_POSITION"),
)

_TABLE_STATS = TableSpec(
    name="information_schema_tables",
    columns=("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "LAST_DDL", "LAST_ALTERED"),
    key=("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME"),
    order_by=("TABLE_NAME",),
)

_VIEW_STATS = TableSpec(
    name="information_schema_views",
    columns=("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "LAST_DDL", "LAST_ALTERED"),
    key=("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME"),
    order_by=("TABLE_NAME",),
)

_MATERIALIZED_VIEW_STATS = TableSpec(
    name="show_materialized_views",
    columns=("database_name", "schema_name", "name", "rows", "bytes"),
    key=("database_name", "schema_name", "name"),
    order_by=("name",),
)

_PIPE_ACTIVITY = TableSpec(
    name="account_usage_copy_history",
    columns=(
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
        "FIRST_ERROR_MESSAGE",
        "FIRST_ERROR_LINE_NUMBER",
    ),
    # One row per pipe (the QUALIFY in queries.pipe_activity keeps only the
    # latest, and a window pass keeps only the newest per pipe), not per
    # historical load -- see ScopeType.PIPE_ACTIVITY.
    key=(
        "PIPE_CATALOG_NAME",
        "PIPE_SCHEMA_NAME",
        "PIPE_NAME",
        "FILE_NAME",
        "LAST_LOAD_TIME",
    ),
    order_by=("PIPE_NAME", "LAST_LOAD_TIME"),
    log=LogShape(
        window_time="LAST_LOAD_TIME",
        newest_by="LAST_LOAD_TIME",
        latest_per=("PIPE_NAME",),
    ),
)

_PIPE_ACTIVITY_HISTORY = TableSpec(
    name="account_usage_copy_history_detail",
    columns=(
        "PIPE_CATALOG_NAME",
        "PIPE_SCHEMA_NAME",
        "PIPE_NAME",
        "FILE_NAME",
        "FILE_SIZE",
        "ROW_COUNT",
        "STATUS",
        "LAST_LOAD_TIME",
        "PIPE_RECEIVED_TIME",
        "TABLE_CATALOG_NAME",
        "TABLE_SCHEMA_NAME",
        "TABLE_NAME",
        "FIRST_ERROR_MESSAGE",
        "FIRST_ERROR_LINE_NUMBER",
    ),
    # _scope disambiguates by pipe -- same reasoning as _PIPE_HISTORY below.
    key=("_scope", "FILE_NAME", "LAST_LOAD_TIME"),
    order_by=("LAST_LOAD_TIME",),
    log=LogShape(window_time="LAST_LOAD_TIME", newest_by="LAST_LOAD_TIME"),
)

_PIPE_HISTORY = TableSpec(
    name="information_schema_copy_history",
    columns=(
        "FILE_NAME",
        "FILE_SIZE",
        "ROW_COUNT",
        "STATUS",
        "LAST_LOAD_TIME",
        "PIPE_RECEIVED_TIME",
        "PIPE_NAME",
        "FIRST_ERROR_MESSAGE",
        "FIRST_ERROR_LINE_NUMBER",
    ),
    # _scope disambiguates by pipe -- the physical table holds every pipe's
    # rows together, same as every other mirror table.
    key=("_scope", "FILE_NAME", "LAST_LOAD_TIME"),
    order_by=("LAST_LOAD_TIME",),
)

_PIPE_STATUS = TableSpec(
    name="system_pipe_status",
    columns=("PIPE_CATALOG_NAME", "PIPE_SCHEMA_NAME", "PIPE_NAME", "PIPE_STATUS"),
    key=("PIPE_CATALOG_NAME", "PIPE_SCHEMA_NAME", "PIPE_NAME"),
    order_by=("PIPE_NAME",),
)

_TASK_ACTIVITY = TableSpec(
    name="account_usage_task_history",
    columns=(
        "NAME",
        "DATABASE_NAME",
        "SCHEMA_NAME",
        "QUERY_ID",
        "SCHEDULED_TIME",
        "ATTEMPT_NUMBER",
        "QUERY_START_TIME",
        "COMPLETED_TIME",
        "STATE",
        "ERROR_CODE",
        "ERROR_MESSAGE",
    ),
    # One row per task (the QUALIFY in queries.task_activity keeps only the
    # latest, and a window pass keeps only the newest per task), not per
    # historical run -- see ScopeType.TASK_ACTIVITY. Not keyed by QUERY_ID,
    # which can be NULL -- see schema.sql.
    key=("DATABASE_NAME", "SCHEMA_NAME", "NAME", "SCHEDULED_TIME", "ATTEMPT_NUMBER"),
    order_by=("NAME", "SCHEDULED_TIME"),
    # Windowed by COMPLETED_TIME: ACCOUNT_USAGE lists a run only once it has
    # finished, so a long run appears carrying a SCHEDULED_TIME from hours
    # before -- see queries.task_activity.
    log=LogShape(
        window_time="COMPLETED_TIME",
        newest_by="SCHEDULED_TIME",
        latest_per=("NAME",),
    ),
)

_TASK_ACTIVITY_HISTORY = TableSpec(
    name="account_usage_task_history_detail",
    columns=(
        "NAME",
        "DATABASE_NAME",
        "SCHEMA_NAME",
        "QUERY_ID",
        "SCHEDULED_TIME",
        "ATTEMPT_NUMBER",
        "QUERY_START_TIME",
        "COMPLETED_TIME",
        "STATE",
        "ERROR_CODE",
        "ERROR_MESSAGE",
    ),
    # _scope disambiguates by task -- same reasoning as _TASK_HISTORY below.
    # Not QUERY_ID, which can be NULL -- see _TASK_ACTIVITY.
    key=("_scope", "SCHEDULED_TIME", "ATTEMPT_NUMBER"),
    order_by=("SCHEDULED_TIME",),
    log=LogShape(window_time="COMPLETED_TIME", newest_by="SCHEDULED_TIME"),
)

_TASK_HISTORY = TableSpec(
    name="information_schema_task_history",
    columns=(
        "NAME",
        "QUERY_ID",
        "SCHEDULED_TIME",
        "ATTEMPT_NUMBER",
        "QUERY_START_TIME",
        "COMPLETED_TIME",
        "STATE",
        "ERROR_CODE",
        "ERROR_MESSAGE",
    ),
    # _scope disambiguates by task -- the physical table holds every task's
    # rows together, same as every other mirror table. Not QUERY_ID, which a
    # run that has not started yet lacks -- see _TASK_ACTIVITY.
    key=("_scope", "SCHEDULED_TIME", "ATTEMPT_NUMBER"),
    order_by=("SCHEDULED_TIME",),
)

_GRANTS = TableSpec(
    name="show_grants",
    columns=(
        "created_on",
        "privilege",
        "granted_on",
        "name",
        "granted_to",
        "grantee_name",
        "grant_option",
        "granted_by",
        "granted_by_role_type",
    ),
    # No natural single-column key -- see schema.sql's own comment.
    key=(
        "_scope",
        "granted_to",
        "grantee_name",
        "privilege",
        "granted_on",
        "name",
        "granted_by",
        "created_on",
    ),
    order_by=("granted_to", "grantee_name", "privilege"),
)

_TAG_REFERENCES = TableSpec(
    name="information_schema_tag_references",
    columns=("TAG_DATABASE", "TAG_SCHEMA", "TAG_NAME", "TAG_VALUE", "DOMAIN"),
    key=("_scope", "TAG_DATABASE", "TAG_SCHEMA", "TAG_NAME", "TAG_VALUE"),
    order_by=("TAG_NAME", "TAG_VALUE"),
)

_COLUMN_TAG_REFERENCES = TableSpec(
    name="information_schema_tag_references_all_columns",
    columns=("TAG_DATABASE", "TAG_SCHEMA", "TAG_NAME", "TAG_VALUE", "COLUMN_NAME"),
    key=(
        "_scope",
        "COLUMN_NAME",
        "TAG_DATABASE",
        "TAG_SCHEMA",
        "TAG_NAME",
        "TAG_VALUE",
    ),
    order_by=("COLUMN_NAME", "TAG_NAME", "TAG_VALUE"),
)

_DATA_PREVIEW = TableSpec(
    name="data_preview",
    columns=("_row_number", "_values"),
    key=("_scope", "_row_number"),
    order_by=("_row_number",),
)


def spec_for(scope: Scope) -> TableSpec:
    match scope.type:
        case ScopeType.DATABASES:
            return _DATABASES
        case ScopeType.SCHEMAS:
            return _SCHEMAS
        case ScopeType.OBJECTS:
            assert scope.object_type is not None
            return _OBJECT_TABLES[scope.object_type]
        case ScopeType.COLUMNS:
            return _COLUMNS
        case ScopeType.TABLE_STATS:
            return _TABLE_STATS
        case ScopeType.VIEW_STATS:
            return _VIEW_STATS
        case ScopeType.MATERIALIZED_VIEW_STATS:
            return _MATERIALIZED_VIEW_STATS
        case ScopeType.PIPE_ACTIVITY:
            return _PIPE_ACTIVITY
        case ScopeType.PIPE_ACTIVITY_HISTORY:
            return _PIPE_ACTIVITY_HISTORY
        case ScopeType.PIPE_HISTORY:
            return _PIPE_HISTORY
        case ScopeType.PIPE_STATUS:
            return _PIPE_STATUS
        case ScopeType.TASK_ACTIVITY:
            return _TASK_ACTIVITY
        case ScopeType.TASK_ACTIVITY_HISTORY:
            return _TASK_ACTIVITY_HISTORY
        case ScopeType.TASK_HISTORY:
            return _TASK_HISTORY
        case ScopeType.GRANTS:
            return _GRANTS
        case ScopeType.TAG_REFERENCES:
            return _TAG_REFERENCES
        case ScopeType.COLUMN_TAG_REFERENCES:
            return _COLUMN_TAG_REFERENCES
        case ScopeType.DATA_PREVIEW:
            return _DATA_PREVIEW
        case ScopeType.WAREHOUSES:
            return _WAREHOUSES
        case ScopeType.ROLES:
            return _ROLES
        case ScopeType.DATABASE_PRIME:
            # A prime is a fetch strategy, not a set of rows: it fills other
            # scopes' tables and owns none of its own.
            raise ValueError(f"{scope.type} has no mirror table")


@dataclass(frozen=True)
class Entry:
    """A scope's freshness, independent of its rows."""

    scope_key: str
    state: CacheState
    generation: int
    fetched_at: float | None
    row_count: int
    error: str | None
    #: The last completed pass hit its statement's row cap, so these rows
    #: are current but not all of them. A view showing this scope is
    #: expected to say so -- see `HistoryView`.
    truncated: bool = False
    #: When the latest pass began, finished or not. See `due`.
    attempted_at: float | None = None
    #: A log scope's coverage: the epoch back to which its rows are complete,
    #: or None before a full pass. See `plan_window`.
    covered_from: float | None = None

    def age(self, now: float | None = None) -> float | None:
        return _since(self.fetched_at, now)

    def due(self, ttl: float, now: float | None = None) -> float | None:
        """Seconds until a background refresh is due for this entry's scope.

        The one answer the sweep and the countdown both read, so they cannot
        disagree. Zero or less once due; None when a `ttl` of 0 disables
        refreshing.

        EMPTY is due at once: a scope claimed by a pass that has not finished
        yet carries the previous pass's fetched_at, so it can read as fresh
        while holding no rows. A failed refresh keeps its old `fetched_at`,
        so it ages from its last attempt instead. Aged by `fetched_at`, it
        would read as overdue forever, and every sweep would rerun the
        failing statement.
        """
        if self.state is CacheState.EMPTY:
            return 0.0
        if self.state is CacheState.ERROR:
            return due_in(_since(self.attempted_at, now), ttl)
        return due_in(self.age(now), ttl)


def _since(moment: float | None, now: float | None) -> float | None:
    if moment is None:
        return None
    return max(0.0, (now if now is not None else time.time()) - moment)


@dataclass(frozen=True)
class Window:
    """How far back one pass of a log scope asks -- see `plan_window`."""

    #: Seconds back from when the statement is sent.
    seconds: int
    #: Whether that is the whole fetch window: a full pass, which replaces
    #: the scope's rows. Otherwise a window pass, which adds to them.
    full: bool


@dataclass(frozen=True)
class Prune:
    """What a window pass removes when it finishes -- see `finish_pass`."""

    #: The epoch before which a row is outside the fetch window.
    cutoff: float
    #: The scope's row cap, or None for a scope without one.
    max_rows: int | None = None


def plan_window(entry: Entry, fetch_seconds: int, margin: float, now: float) -> Window:
    """The window the next pass of a log scope should ask for.

    A log scope's rows are past events that do not change once they appear,
    so a refresh only needs what is newer than the last one. It starts
    `margin` before the last successful pass, because ACCOUNT_USAGE
    publishes a row some time after the event it records, stamped with the
    event's time: a row that was not there yet then would otherwise fall
    before the window for good.

    A full pass instead when nothing says the stored rows reach back as far
    as the fetch window: never fetched, coverage forgotten
    (`Repository.forget_coverage`), or a fetch window raised since. Also
    when the window would be at least as long as the fetch window anyway.
    A failed pass leaves `fetched_at` where it was, so the next window
    reaches back to the last success on its own.
    """
    full = Window(seconds=fetch_seconds, full=True)
    if (
        entry.state is CacheState.EMPTY
        or entry.fetched_at is None
        or entry.covered_from is None
        or entry.covered_from > now - fetch_seconds
    ):
        return full
    back = math.ceil(now - entry.fetched_at + margin)
    if back >= fetch_seconds:
        return full
    return Window(seconds=back, full=False)


def _julian(epoch: float) -> str:
    """An epoch as text `julianday()` reads, for comparing stored times."""
    return datetime.fromtimestamp(epoch, UTC).isoformat(sep=" ")


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
        #: Where DATA_PREVIEW scopes live instead of the file: table data,
        #: which must never reach the disk (see `MemoryDatabase`). One per
        #: repository, so a role switch starts with no previews, the same as
        #: it starts on another file.
        self._previews = MemoryDatabase()

    def _store(self, scope: Scope) -> Database | MemoryDatabase:
        """Which database holds `scope`'s rows and its ledger entry.

        Everything below that takes a scope goes through this, so a caller
        never needs to know a preview is not in the file.
        """
        if scope.type is ScopeType.DATA_PREVIEW:
            return self._previews
        return self._db

    # ---------------------------------------------------------------- reads

    def entry(self, scope: Scope) -> Entry:
        with self._store(scope).connect() as connection:
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
            truncated=bool(row["truncated"]),
            attempted_at=row["attempted_at"],
            covered_from=row["covered_from"],
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
        with self._store(scope).connect() as connection:
            return [dict(row) for row in self._select(connection, scope)]

    def _cached(self, scope: Scope, build: Callable[[sqlite3.Row], Any]) -> Cached[Any]:
        with self._store(scope).connect() as connection:
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
            lambda row: WarehouseRow(
                name=row["name"],
                comment=row["comment"] or "",
                size=row["size"] or "",
                is_default=row["is_default"] == "Y",
            ),
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
        assert scope.object_type is not None
        object_type = scope.object_type

        def build(row: sqlite3.Row) -> SFObject:
            return SFObject(
                database_name=row["database_name"],
                schema_name=row["schema_name"],
                name=row["name"],
                type=object_type,
                comment=row["comment"] or "",
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
                desc_type=row["_desc_type"] or "",
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
        spec = _OBJECT_TABLES[ref.type]
        statement = (
            f"SELECT * FROM {spec.name} "
            "WHERE database_name = ? AND schema_name = ? AND name = ?"
        )
        with self._db.connect() as connection:
            row = connection.execute(
                statement, (ref.database, ref.schema, ref.name)
            ).fetchone()
        return dict(row) if row is not None else None

    def pipe_target_table(
        self, database: str, schema: str, pipe: str
    ) -> tuple[str, str, str] | None:
        """The (catalog, schema, table) a pipe loads into, or None.

        Read first off whatever PIPE_ACTIVITY (ACCOUNT_USAGE.COPY_HISTORY)
        already has cached for this pipe: Snowflake's own resolution, and its
        newest load. Then off the pipe's own PIPE_ACTIVITY_HISTORY, its
        newest load first: the same resolution, which a pipe view opened
        before the schema-wide snapshot has answered gets sooner, since one
        pipe's statement takes seconds where the schema's can take half a
        minute. A pipe with no load in either -- one that never loaded, or
        not within the fetch window -- falls back to its cached SHOW PIPES
        definition, which names the table only when it is fully qualified
        (see `pipe_definition.copy_target`). None means none of them says,
        and callers (PipeView, FetchService._execute) treat it as "nothing to
        show yet", not an error.
        """
        for row in self.raw_rows(pipe_activity_scope(database, schema)):
            if row.get("PIPE_NAME") != pipe:
                continue
            catalog = row.get("TABLE_CATALOG_NAME")
            table_schema = row.get("TABLE_SCHEMA_NAME")
            table = row.get("TABLE_NAME")
            if catalog and table_schema and table:
                return (str(catalog), str(table_schema), str(table))
        backfill = pipe_activity_history_scope(database, schema, pipe)
        with self._store(backfill).connect() as connection:
            row = connection.execute(
                "SELECT TABLE_CATALOG_NAME, TABLE_SCHEMA_NAME, TABLE_NAME "
                f"FROM {spec_for(backfill).name} WHERE _scope = ? "
                "AND TABLE_CATALOG_NAME IS NOT NULL "
                "AND TABLE_SCHEMA_NAME IS NOT NULL AND TABLE_NAME IS NOT NULL "
                "ORDER BY julianday(LAST_LOAD_TIME) DESC LIMIT 1",
                (backfill.key,),
            ).fetchone()
        if row is not None:
            return (
                str(row["TABLE_CATALOG_NAME"]),
                str(row["TABLE_SCHEMA_NAME"]),
                str(row["TABLE_NAME"]),
            )
        detail = self.object_detail(ObjectRef(ObjectType.PIPE, database, schema, pipe))
        definition = (detail or {}).get("definition")
        return copy_target(definition) if definition else None

    def pipe_idle_as_of(self, database: str, schema: str, pipe: str) -> float | None:
        """When PIPE_ACTIVITY last saw no load for this pipe, or None.

        PIPE_ACTIVITY keeps each pipe's latest load within the fetch window,
        so a fetched snapshot without a row for this pipe says that nothing
        loaded in that window up to `fetched_at`. EMPTY is excluded: a pass
        can hold that state with no rows while carrying the previous pass's
        `fetched_at`. A failed or running refresh still keeps the earlier
        rows, so the absence still holds as of `fetched_at`.

        The window is the fetch window as it was set for that pass; raising
        the setting afterwards makes the claim cover less than it says until
        the snapshot is refetched.
        """
        scope = pipe_activity_scope(database, schema)
        with self._store(scope).connect() as connection:
            entry = self._entry(connection, scope)
            rows = self._select(connection, scope)
        if entry.state is CacheState.EMPTY or entry.fetched_at is None:
            return None
        if any(row["PIPE_NAME"] == pipe for row in rows):
            return None
        return entry.fetched_at

    def search_corpus(self, databases: Sequence[str]) -> list[tuple[str, ObjectRef]]:
        """Every searchable object across `databases` as (text, ref) pairs.

        The text is `SCHEMA.NAME`, so typing "sales.ord" narrows by schema and
        object together. Ranking happens in Python: sqlite can match
        substrings but not subsequences, and "stgord" finding
        STG_CUSTOMER_ORDERS is the whole point. The owning database is not
        baked into this text -- search.py adds it as a display-only prefix,
        since letting it compete for the query's leading characters the way
        schema does would dilute matching for no benefit (see docs/DESIGN.md).
        """
        if not databases:
            return []
        placeholders = ", ".join("?" for _ in databases)
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT database_name, schema_name, name, kind FROM search_object "
                f"WHERE database_name IN ({placeholders}) "
                "ORDER BY database_name, schema_name, name",
                tuple(databases),
            ).fetchall()
        corpus: list[tuple[str, ObjectRef]] = []
        for row in rows:
            try:
                object_type = ObjectType(row["kind"])
            except ValueError:
                continue
            corpus.append(
                (
                    f"{row['schema_name']}.{row['name']}",
                    ObjectRef(
                        type=object_type,
                        database=row["database_name"],
                        schema=row["schema_name"],
                        name=row["name"],
                    ),
                )
            )
        return corpus

    def search_column_corpus(
        self, databases: Sequence[str]
    ) -> list[tuple[ObjectRef, str]]:
        """Every cached column across `databases`, paired with its owning object.

        Joined back to `search_object` rather than read off
        `information_schema_columns` alone, so a column only surfaces if its
        owning table/view is itself still known there -- a stale leftover
        column row from a since-dropped table cannot spuriously appear. Only
        TABLE/VIEW ever have columns, but the join excludes other types
        regardless of whether that ever actually matters.

        Currently unused -- SearchDialog.on_mount's call into this is
        commented out, cross-database column search not being ready for
        prime time yet (see docs/DESIGN.md "Accepted design gaps"). Kept multi-database-
        ready anyway, so re-enabling it later is restoring one call, not
        writing new code.
        """
        if not databases:
            return []
        placeholders = ", ".join("?" for _ in databases)
        with self._db.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT o.database_name, o.schema_name, o.name, o.kind, c.COLUMN_NAME
                FROM information_schema_columns c
                JOIN search_object o
                  ON o.database_name = c.TABLE_CATALOG
                 AND o.schema_name  = c.TABLE_SCHEMA
                 AND o.name         = c.TABLE_NAME
                WHERE c.TABLE_CATALOG IN ({placeholders}) AND o.kind IN ('TABLE', 'VIEW')
                ORDER BY o.database_name, o.schema_name, o.name, c.ORDINAL_POSITION
                """,
                tuple(databases),
            ).fetchall()
        # One ObjectRef per column here would mean one allocation per column
        # rather than per table -- 15-20x more than necessary at a typical
        # column-per-table ratio, and it showed: ~30ms of a ~73ms call on a
        # 30k-row corpus was this constructor alone. The ORDER BY above
        # already groups every column under its owner, so consecutive rows
        # sharing a key can just reuse the previous ObjectRef instead.
        corpus: list[tuple[ObjectRef, str]] = []
        current_key: tuple[str, str, str] | None = None
        current_ref: ObjectRef | None = None
        for row in rows:
            key = (row["database_name"], row["schema_name"], row["name"])
            if key != current_key:
                current_key = key
                try:
                    object_type = ObjectType(row["kind"])
                except ValueError:
                    current_ref = None
                else:
                    current_ref = ObjectRef(
                        type=object_type,
                        database=row["database_name"],
                        schema=row["schema_name"],
                        name=row["name"],
                    )
            if current_ref is not None:
                corpus.append((current_ref, row["COLUMN_NAME"]))
        return corpus

    def search_schema_corpus(self, databases: Sequence[str]) -> list[SchemaRef]:
        """Every cached schema across `databases`, for the search modal.

        Schemas are already flat and singly-sourced (`show_schemas`), unlike
        objects and columns, so this is a thin wrapper over `schemas()`
        rather than its own query -- kept anyway, so the modal reads three
        parallel `search_*_corpus` methods instead of building one of its
        three corpora by hand. Looping per database rather than one combined
        query: `schemas()` is already a cheap cached per-database lookup, not
        a fresh round trip each time.
        """
        return [
            SchemaRef(database=database, name=row.name)
            for database in databases
            for row in self.schemas(schemas_scope(database)).rows
        ]

    def child_count(self, scope: Scope) -> int:
        """The count behind a collapsed tree label, e.g. `Schemas (40)`."""
        spec = spec_for(scope)
        statement = f"SELECT COUNT(*) AS n FROM {spec.name} WHERE _scope = ?"
        with self._store(scope).connect() as connection:
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
        return [
            scope
            for scope, entry in self._entries(among)
            if (due := entry.due(ttl, now)) is not None and due <= 0
        ]

    def next_due(self, ttl: float, among: Iterable[Scope]) -> float | None:
        """Seconds until the first of `among` is due a background refresh.

        What the footer countdown shows: nothing it watches goes stale before
        this reaches zero. Zero or less for anything already due; None when
        nothing counts, or when a `ttl` of 0 disables refreshing.
        """
        if ttl <= 0:
            return None
        now = time.time()
        due = [entry.due(ttl, now) for _, entry in self._entries(among)]
        return min((d for d in due if d is not None), default=None)

    def failed_scopes(self, among: Iterable[Scope]) -> list[Scope]:
        """Which of `among` failed their last refresh."""
        return [
            scope
            for scope, entry in self._entries(among)
            if entry.state is CacheState.ERROR
        ]

    def unfetched_scopes(self, among: Iterable[Scope]) -> list[Scope]:
        """Which of `among` have never completed a fetch."""
        return [
            scope for scope, entry in self._entries(among) if entry.fetched_at is None
        ]

    def _entries(self, among: Iterable[Scope]) -> Iterator[tuple[Scope, Entry]]:
        """Each scope's ledger entry, read from whichever store holds it."""
        with self._db.connect() as cached, self._previews.connect() as previews:
            for scope in among:
                store = self._store(scope)
                connection = previews if store is self._previews else cached
                yield scope, self._entry(connection, scope)

    # --------------------------------------------------------------- writes

    def begin_pass(self, scope: Scope, started_at: float | None = None) -> int:
        """Claim the scope for a new refresh pass and return its generation.

        `fetched_at` is deliberately carried over rather than cleared: until
        this pass finishes, the age the status bar shows is still the age of
        the rows actually on screen.

        `started_at` is when the statement behind this pass was sent, for a
        caller that only claims the pass once its result is back (see
        `replace_grouped`). It defaults to now.
        """
        if started_at is None:
            started_at = time.time()
        with self._store(scope).connect() as connection:
            entry = self._entry(connection, scope)
            generation = entry.generation + 1
            connection.execute(
                """
                INSERT INTO cache_entry
                    (scope, state, generation, fetched_at, attempted_at,
                     next_cursor, row_count, error, truncated)
                VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, 0)
                ON CONFLICT(scope) DO UPDATE SET
                    state = excluded.state,
                    generation = excluded.generation,
                    attempted_at = excluded.attempted_at,
                    next_cursor = NULL,
                    row_count = 0,
                    error = NULL,
                    truncated = 0
                """,
                (
                    scope.key,
                    CacheState.PARTIAL.value,
                    generation,
                    entry.fetched_at,
                    started_at,
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
        with self._store(scope).connect() as connection:
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

    def finish_pass(
        self,
        scope: Scope,
        generation: int,
        *,
        truncated: bool = False,
        covered_from: float | None = None,
        prune: Prune | None = None,
    ) -> bool:
        """Delete the rows this pass is responsible for, and mark the scope
        complete.

        A full pass is responsible for the whole scope, so every row it did
        not rewrite is swept. A window pass (`prune` set, for a log scope) is
        responsible only for what its window supersedes, because its
        statement asked only for what is new: nothing it did not return is
        swept. Instead, rows older than the fetch window go, a scope reduced
        to one row per object keeps only the newest per object, and a capped
        one keeps only its newest `max_rows`. Rows still never disappear
        before this point, and a failed pass still deletes nothing.

        A window pass that hit its own cap is swept like a full one. The
        newest rows in its window are then the newest rows overall, which is
        exactly what a full pass would have kept, and keeping older rows
        would leave a hole between them.

        `truncated` records that the statement behind this pass hit its own
        row cap. The pass is still a complete, swept, COMPLETE one -- the
        rows written are the newest and entirely valid -- so this is not an
        error state; it is the difference between 'this is the history' and
        'this is as much of the history as was asked for'. A window pass
        that leaves its scope holding the cap's worth of rows says so too,
        on the same reasoning as `jobs._capped`.

        `covered_from` is a full pass's coverage, stored for `plan_window`.
        None leaves the stored value alone, so a window pass keeps the
        coverage it extended, and a `forget_coverage` that landed while it
        ran still holds.

        The rows count as fetched when the pass began, not now. Its first
        page is that old, and stamping the end would let a long pass's oldest
        rows go stale before the countdown says so.
        """
        spec = spec_for(scope)
        sweep = f"DELETE FROM {spec.name} WHERE _scope = ? AND _generation < ?"
        count_rows = f"SELECT COUNT(*) AS n FROM {spec.name} WHERE _scope = ?"
        with self._store(scope).connect() as connection:
            if self._entry(connection, scope).generation != generation:
                return False
            if prune is None or truncated:
                connection.execute(sweep, (scope.key, generation))
            else:
                self._prune(connection, spec, scope, prune)
            count = connection.execute(count_rows, (scope.key,)).fetchone()["n"]
            if prune is not None and prune.max_rows is not None:
                truncated = truncated or count >= prune.max_rows
            connection.execute(
                """
                UPDATE cache_entry
                   SET state = ?, fetched_at = attempted_at, next_cursor = NULL,
                       row_count = ?, error = NULL, truncated = ?,
                       covered_from = COALESCE(?, covered_from)
                 WHERE scope = ?
                """,
                (
                    CacheState.COMPLETE.value,
                    count,
                    int(truncated),
                    covered_from,
                    scope.key,
                ),
            )
        return True

    @staticmethod
    def _prune(
        connection: sqlite3.Connection, spec: TableSpec, scope: Scope, prune: Prune
    ) -> None:
        """What a window pass deletes instead of a sweep -- see `finish_pass`."""
        assert spec.log is not None, f"{scope.type} is not a log scope"
        window_time = _quote(spec.log.window_time)
        # A tie goes to the row written last.
        newest = f"julianday({_quote(spec.log.newest_by)}) DESC, rowid DESC"
        connection.execute(
            f"DELETE FROM {spec.name} "
            f"WHERE _scope = ? AND julianday({window_time}) < julianday(?)",
            (scope.key, _julian(prune.cutoff)),
        )
        if spec.log.latest_per is not None:
            group = ", ".join(_quote(column) for column in spec.log.latest_per)
            connection.execute(
                f"DELETE FROM {spec.name} WHERE _scope = ? AND rowid NOT IN ("
                "SELECT rowid FROM ("
                f"SELECT rowid, ROW_NUMBER() OVER (PARTITION BY {group} "
                f"ORDER BY {newest}) AS n FROM {spec.name} WHERE _scope = ?"
                ") WHERE n = 1)",
                (scope.key, scope.key),
            )
        if prune.max_rows is not None:
            connection.execute(
                f"DELETE FROM {spec.name} WHERE _scope = ? AND rowid NOT IN ("
                f"SELECT rowid FROM {spec.name} WHERE _scope = ? "
                f"ORDER BY {newest} LIMIT ?)",
                (scope.key, scope.key, prune.max_rows),
            )

    def forget_coverage(self, scope: Scope) -> None:
        """Make the next pass of a log scope a full one.

        The escape hatch for a window pass that missed a row: one that
        ACCOUNT_USAGE published later than the lag margin, which no later
        window asks for again. The rows stay on screen until that full pass
        replaces them. Cleared before the pass rather than requested with it,
        so a full pass that fails is retried as a full pass too.
        """
        with self._store(scope).connect() as connection:
            connection.execute(
                "UPDATE cache_entry SET covered_from = NULL WHERE scope = ?",
                (scope.key,),
            )

    def replace_grouped(
        self,
        grouped: Mapping[Scope, Sequence[dict[str, Any]]],
        started_at: float | None = None,
    ) -> None:
        """Run a complete pass for each scope from one already-fetched result.

        Used by the database-wide prime: one SHOW returns every schema's
        objects, so each schema's scope is begun, written and swept here
        rather than each costing its own round trip.

        Scopes mapped to an empty list still get a full pass. That is the
        point of passing them: a schema with no tables has to read as
        "fetched, none" rather than "never fetched", or the tree would keep
        asking for it and the node would never settle.

        `started_at` is when that one statement was sent, which is how old
        every scope's rows are.
        """
        for scope, rows in grouped.items():
            generation = self.begin_pass(scope, started_at)
            self.write_page(scope, generation, rows, None)
            self.finish_pass(scope, generation)

    def fail_pass(self, scope: Scope, generation: int, error: str) -> bool:
        """Record a failure without sweeping.

        Stale rows stay visible: showing yesterday's tables beats showing an
        empty schema because the warehouse happened to be unreachable.
        """
        with self._store(scope).connect() as connection:
            if self._entry(connection, scope).generation != generation:
                return False
            connection.execute(
                "UPDATE cache_entry SET state = ?, error = ? WHERE scope = ?",
                (CacheState.ERROR.value, error, scope.key),
            )
        return True
