# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Domain vocabulary: frozen dataclasses and enums.

Deliberately free of Textual and of snowflake.connector types, so widgets
never touch a driver object and every module here is testable without either
dependency installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ObjectType(StrEnum):
    """A type of Snowflake object that nivaria can list and show.

    The value is the noun Snowflake itself uses (as in `SHOW TABLES`), so it
    can be dropped straight into a statement.
    """

    TABLE = "TABLE"
    VIEW = "VIEW"
    PIPE = "PIPE"
    STAGE = "STAGE"
    TASK = "TASK"

    @property
    def plural(self) -> str:
        """The lowercase plural Snowflake uses, e.g. `tables`."""
        return f"{self.value.lower()}s"

    @property
    def title(self) -> str:
        """Title-cased plural for a tree category label, e.g. `Tables`."""
        return self.plural.title()

    @property
    def pageable(self) -> bool:
        """Whether `SHOW <plural>` actually honours `LIMIT`/`FROM`.

        Not a guess and not a documented property: verified statement by
        statement against two unrelated live accounts. `SHOW PIPES` and `SHOW
        STAGES` accept both clauses and silently ignore them, returning every
        row in the schema or database regardless -- which would make the keyset
        continuation non-terminating, since every "next page" comes back
        identical to the last. Declared here rather than
        discovered at runtime, so the statement builder can stop asking for
        something that does not work instead of the scheduler having to
        recover from it. See docs/DESIGN.md, "Bounding a result".

        `SHOW TASKS` pages, verified live too, but its `FROM` is inclusive:
        a page starts with the cursor row. `jobs.fetch_page` drops that echo,
        so it is pageable all the same.
        """
        return self not in (ObjectType.PIPE, ObjectType.STAGE)


class ScopeType(StrEnum):
    """What a cache scope covers: one fetchable, separately-refreshable set."""

    DATABASES = "databases"
    SCHEMAS = "schemas"
    OBJECTS = "objects"
    COLUMNS = "columns"
    #: Not a set of rows of its own: a request to fill every schema's OBJECTS
    #: scope in one database from a single statement. It exists as a scope so
    #: the scheduler can deduplicate and cancel it like any other work.
    DATABASE_PRIME = "prime"
    #: A schema's tables' LAST_DDL/LAST_ALTERED, sourced by a real SELECT
    #: against INFORMATION_SCHEMA.TABLES rather than SHOW -- a deliberate
    #: exception to SHOW-only (see docs/DESIGN.md). Scoped to
    #: (database, schema) like OBJECTS, but backed by its own mirror table.
    TABLE_STATS = "table_stats"
    #: A schema's views' LAST_DDL/LAST_ALTERED, the same INFORMATION_SCHEMA.
    #: TABLES statement TABLE_STATS uses, just bound to TABLE_TYPE = 'VIEW'
    #: instead of 'BASE TABLE'. A separate ScopeType rather than an
    #: object_type on TABLE_STATS: the two are separately refreshable, so
    #: they are separate scopes.
    VIEW_STATS = "view_stats"
    #: A schema's materialized views' Rows/Bytes, sourced by SHOW MATERIALIZED
    #: VIEWS -- a distinct Snowflake command from SHOW VIEWS (see
    #: show_materialized_views in schema.sql), with its own column set that
    #: actually carries row/byte stats, which SHOW VIEWS never does even for
    #: materialized views. Metadata layer like SHOW VIEWS, so -- unlike
    #: TABLE_STATS/VIEW_STATS -- this needs no warehouse.
    MATERIALIZED_VIEW_STATS = "materialized_view_stats"
    #: A schema's stages' LAST_ALTERED, sourced by a real SELECT against
    #: INFORMATION_SCHEMA.STAGES because SHOW STAGES has no such timestamp --
    #: a second source like TABLE_STATS, needing a warehouse the same way.
    STAGE_STATS = "stage_stats"
    #: A schema's pipes' *latest* load each (bounded by the "pipe history
    #: fetch window" setting), sourced by a real SELECT against
    #: SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY rather than SHOW -- a second source,
    #: like TABLE_STATS (see docs/DESIGN.md). Scoped to (database, schema) like
    #: OBJECTS, but backed by its own mirror table. A log scope: after its first full pass, each
    #: refresh asks only for what is new (`repository.plan_window`), which is
    #: what makes the ordinary cache age affordable for a statement whose full
    #: form takes seconds. Reduced to one row per pipe in SQL (see
    #: queries.pipe_activity) -- a single pipe's own full history is
    #: PIPE_ACTIVITY_HISTORY below, not this scope.
    PIPE_ACTIVITY = "pipe_activity"
    #: One pipe's full ACCOUNT_USAGE.COPY_HISTORY backfill (bounded by the
    #: "pipe history fetch window" setting, same as PIPE_ACTIVITY), scoped to
    #: one pipe like PIPE_HISTORY below -- fetched lazily, only once that
    #: pipe's detail view is open, so a schema full of pipes never pays for
    #: more history than whichever pipes someone has actually looked at.
    #: Unlike PIPE_HISTORY, no target-table resolution is needed to fetch
    #: this: ACCOUNT_USAGE.COPY_HISTORY has an ordinary PIPE_NAME column.
    PIPE_ACTIVITY_HISTORY = "pipe_activity_history"
    #: One pipe's near-real-time load history (the last 14 days -- Snowflake's
    #: own retention limit for the function backing it), sourced by a real
    #: SELECT against INFORMATION_SCHEMA.COPY_HISTORY. Scoped to one pipe like
    #: COLUMNS is scoped to one table -- see `pipe_history_scope`.
    PIPE_HISTORY = "pipe_history"
    #: Every pipe's SYSTEM$PIPE_STATUS in a schema. The function takes one
    #: pipe as a constant, so a pass costs server time per pipe and pages
    #: through the cached SHOW PIPES names in batches (see
    #: queries.pipe_status). Needs no warehouse.
    PIPE_STATUS = "pipe_status"
    #: Every task's *latest* run in a schema (bounded by the "task history
    #: fetch window" setting), sourced by a real SELECT against
    #: SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY rather than SHOW -- a second source
    #: like PIPE_ACTIVITY (see docs/DESIGN.md), just against a different
    #: ACCOUNT_USAGE view. Scoped to (database, schema)
    #: like PIPE_ACTIVITY, but backed by its own mirror table.
    #: Reduced to one row per task in SQL (see queries.task_activity) -- a
    #: single task's own full run history is TASK_ACTIVITY_HISTORY below. A
    #: log scope, like PIPE_ACTIVITY.
    TASK_ACTIVITY = "task_activity"
    #: One task's full ACCOUNT_USAGE.TASK_HISTORY backfill (bounded by the
    #: "task history fetch window" setting, same as TASK_ACTIVITY), scoped to
    #: one task like TASK_HISTORY below -- fetched lazily, only once that
    #: task's detail view is open. No target-table resolution is needed to
    #: fetch this: ACCOUNT_USAGE.TASK_HISTORY has an ordinary NAME column.
    TASK_ACTIVITY_HISTORY = "task_activity_history"
    #: One task's near-real-time run history (the last 7 days -- Snowflake's
    #: own hard retention limit for the function backing it, shorter than
    #: PIPE_HISTORY's 14), sourced by a real SELECT against
    #: INFORMATION_SCHEMA.TASK_HISTORY. Scoped to one task like PIPE_HISTORY
    #: is scoped to one pipe -- see `task_history_scope`. Unlike PIPE_HISTORY,
    #: this needs no target-table resolution to fetch: the function accepts
    #: TASK_NAME/DATABASE_NAME/SCHEMA_NAME as direct named arguments, all of
    #: which a task's own Scope already carries.
    TASK_HISTORY = "task_history"
    #: Every grant on one schema or one database (schema is None), sourced by
    #: SHOW GRANTS ON SCHEMA/DATABASE -- metadata layer, needs no warehouse,
    #: same as every plain SHOW scope. With `table` set it is one object's
    #: grants instead (SHOW GRANTS ON <object_type>), which has no schema-wide
    #: form and so costs one round trip per object.
    #: granted_to can be ROLE, DATABASE ROLE, USER, SHARE or APPLICATION
    #: ROLE -- deliberately not filtered down to roles only.
    GRANTS = "grants"
    #: Tags applied to one schema or one database (schema is None), sourced
    #: by a real SELECT against INFORMATION_SCHEMA.TAG_REFERENCES rather than
    #: SHOW -- a second source, like TABLE_STATS (see docs/DESIGN.md). Needs a
    #: running warehouse; degrades blank without one, same as every SELECT-backed
    #: scope.
    TAG_REFERENCES = "tag_references"
    #: One table's (or view's) columns' tags, sourced by a real SELECT
    #: against INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS -- a different
    #: function from TAG_REFERENCES above, purpose-built to return every tag
    #: on every column of one table in a single call rather than needing one
    #: TAG_REFERENCES call per column. Scoped to (database, schema, table)
    #: like COLUMNS. Needs a running warehouse; degrades blank without one,
    #: same as TAG_REFERENCES.
    COLUMN_TAG_REFERENCES = "column_tag_references"
    #: Some of one table's (or view's) rows (Snowsight calls this a data preview),
    #: sourced by `SELECT * ... LIMIT`. Scoped to (database, schema, table)
    #: like COLUMNS. Needs a running warehouse. The only scope holding table
    #: data rather than metadata, which is why `Repository` keeps it in a
    #: `MemoryDatabase` instead of the cache file.
    DATA_PREVIEW = "data_preview"
    WAREHOUSES = "warehouses"
    ROLES = "roles"


class CacheState(StrEnum):
    EMPTY = "empty"
    PARTIAL = "partial"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass(frozen=True)
class Scope:
    """One separately-refreshable set of rows in the cache.

    A scope is the unit of everything: what the repository reads, what the
    freshness ledger ages, what a fetch job refills, and what a CacheUpdated
    message names. Keeping it a single hashable value is what lets dedup,
    routing and staleness all be dictionary lookups.
    """

    type: ScopeType
    database: str | None = None
    schema: str | None = None
    #: Which object type an OBJECTS listing or a DATABASE_PRIME is for, or
    #: which kind of object an object-level GRANTS scope names (its statement
    #: says `SHOW GRANTS ON TABLE`, not just `ON`). Every other scope type
    #: leaves it unset. Routing keys off the scope *type*, never off this
    #: field being set -- see `filling_prime`.
    object_type: ObjectType | None = None
    #: The single object a scope narrows to, whatever it is called for that
    #: scope type: a table for COLUMNS, a pipe for PIPE_HISTORY, a task for
    #: TASK_HISTORY. Named for the first use rather than renamed per scope.
    table: str | None = None

    @property
    def key(self) -> str:
        """The stable string stored in `cache_entry.scope`.

        Chosen to be readable when eyeballing the sqlite file. It is treated
        as opaque -- nothing parses it back into a Scope, so a database or
        schema name containing a dot cannot cause an ambiguity.
        """
        match self.type:
            case ScopeType.DATABASES:
                return "databases"
            case ScopeType.SCHEMAS:
                return f"schemas:{self.database}"
            case ScopeType.OBJECTS:
                assert self.object_type is not None
                return f"{self.object_type.plural}:{self.database}.{self.schema}"
            case ScopeType.COLUMNS:
                return f"columns:{self.database}.{self.schema}.{self.table}"
            case ScopeType.DATABASE_PRIME:
                assert self.object_type is not None
                return f"{self.object_type.plural}@{self.database}"
            case ScopeType.TABLE_STATS:
                return f"table_stats:{self.database}.{self.schema}"
            case ScopeType.VIEW_STATS:
                return f"view_stats:{self.database}.{self.schema}"
            case ScopeType.MATERIALIZED_VIEW_STATS:
                return f"materialized_view_stats:{self.database}.{self.schema}"
            case ScopeType.STAGE_STATS:
                return f"stage_stats:{self.database}.{self.schema}"
            case ScopeType.PIPE_ACTIVITY:
                return f"pipe_activity:{self.database}.{self.schema}"
            case ScopeType.PIPE_ACTIVITY_HISTORY:
                return (
                    f"pipe_activity_history:{self.database}.{self.schema}.{self.table}"
                )
            case ScopeType.PIPE_HISTORY:
                return f"pipe_history:{self.database}.{self.schema}.{self.table}"
            case ScopeType.PIPE_STATUS:
                return f"pipe_status:{self.database}.{self.schema}"
            case ScopeType.TASK_ACTIVITY:
                return f"task_activity:{self.database}.{self.schema}"
            case ScopeType.TASK_ACTIVITY_HISTORY:
                return (
                    f"task_activity_history:{self.database}.{self.schema}.{self.table}"
                )
            case ScopeType.TASK_HISTORY:
                return f"task_history:{self.database}.{self.schema}.{self.table}"
            case ScopeType.GRANTS:
                if self.table is not None:
                    # The type is part of the key: a pipe may share a
                    # table's name, and each has its own grants.
                    assert self.object_type is not None
                    return (
                        f"grants:{self.object_type.plural}:"
                        f"{self.database}.{self.schema}.{self.table}"
                    )
                if self.schema is None:
                    return f"grants:{self.database}"
                return f"grants:{self.database}.{self.schema}"
            case ScopeType.TAG_REFERENCES:
                if self.schema is None:
                    return f"tag_references:{self.database}"
                return f"tag_references:{self.database}.{self.schema}"
            case ScopeType.COLUMN_TAG_REFERENCES:
                return (
                    f"column_tag_references:{self.database}.{self.schema}.{self.table}"
                )
            case ScopeType.DATA_PREVIEW:
                return f"data_preview:{self.database}.{self.schema}.{self.table}"
            case ScopeType.WAREHOUSES:
                return "warehouses"
            case ScopeType.ROLES:
                return "roles"

    def __str__(self) -> str:
        return self.key


def databases_scope() -> Scope:
    return Scope(ScopeType.DATABASES)


def schemas_scope(database: str) -> Scope:
    return Scope(ScopeType.SCHEMAS, database=database)


def objects_scope(object_type: ObjectType, database: str, schema: str) -> Scope:
    return Scope(
        ScopeType.OBJECTS, database=database, schema=schema, object_type=object_type
    )


def columns_scope(database: str, schema: str, table: str) -> Scope:
    return Scope(ScopeType.COLUMNS, database=database, schema=schema, table=table)


def prime_scope(object_type: ObjectType, database: str) -> Scope:
    return Scope(ScopeType.DATABASE_PRIME, database=database, object_type=object_type)


def filling_prime(scope: Scope) -> Scope | None:
    """The prime whose one statement also fills `scope`, if any.

    A prime writes other scopes' rows, never its own, so whether `scope` is
    being fetched depends on that prime too. Only a DATABASE_PRIME stands in
    for a request the UI makes (`main._requestable`).
    """
    if scope.type is not ScopeType.OBJECTS:
        return None
    assert scope.object_type is not None
    assert scope.database is not None
    return prime_scope(scope.object_type, scope.database)


def table_stats_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.TABLE_STATS, database=database, schema=schema)


def view_stats_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.VIEW_STATS, database=database, schema=schema)


def materialized_view_stats_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.MATERIALIZED_VIEW_STATS, database=database, schema=schema)


def stage_stats_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.STAGE_STATS, database=database, schema=schema)


def pipe_activity_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.PIPE_ACTIVITY, database=database, schema=schema)


def pipe_activity_history_scope(database: str, schema: str, pipe: str) -> Scope:
    # `table` holds the pipe's name -- same reuse `pipe_history_scope` makes.
    return Scope(
        ScopeType.PIPE_ACTIVITY_HISTORY, database=database, schema=schema, table=pipe
    )


def pipe_history_scope(database: str, schema: str, pipe: str) -> Scope:
    # `table` holds the pipe's name here -- the generic "one object this
    # scope narrows to" slot, the same reuse `columns_scope` makes for a
    # table's name.
    return Scope(ScopeType.PIPE_HISTORY, database=database, schema=schema, table=pipe)


def pipe_status_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.PIPE_STATUS, database=database, schema=schema)


def task_activity_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.TASK_ACTIVITY, database=database, schema=schema)


def task_activity_history_scope(database: str, schema: str, task: str) -> Scope:
    # `table` holds the task's name -- same reuse `pipe_activity_history_scope`
    # makes for the pipe's name.
    return Scope(
        ScopeType.TASK_ACTIVITY_HISTORY, database=database, schema=schema, table=task
    )


def task_history_scope(database: str, schema: str, task: str) -> Scope:
    # `table` holds the task's name here -- same reuse `pipe_history_scope`
    # makes for the pipe's name.
    return Scope(ScopeType.TASK_HISTORY, database=database, schema=schema, table=task)


def grants_scope(database: str, schema: str | None = None) -> Scope:
    # schema=None means a database-level grants scope, schema set means
    # schema-level.
    return Scope(ScopeType.GRANTS, database=database, schema=schema)


def object_grants_scope(ref: ObjectRef) -> Scope:
    # `table` holds the object's name -- the same reuse `pipe_history_scope`
    # makes -- and `object_type` which kind it is, since SHOW GRANTS ON needs
    # the noun.
    return Scope(
        ScopeType.GRANTS,
        database=ref.database,
        schema=ref.schema,
        object_type=ref.type,
        table=ref.name,
    )


def tag_references_scope(database: str, schema: str | None = None) -> Scope:
    return Scope(ScopeType.TAG_REFERENCES, database=database, schema=schema)


def column_tag_references_scope(database: str, schema: str, table: str) -> Scope:
    return Scope(
        ScopeType.COLUMN_TAG_REFERENCES, database=database, schema=schema, table=table
    )


def data_preview_scope(database: str, schema: str, table: str) -> Scope:
    return Scope(ScopeType.DATA_PREVIEW, database=database, schema=schema, table=table)


def warehouses_scope() -> Scope:
    return Scope(ScopeType.WAREHOUSES)


def roles_scope() -> Scope:
    return Scope(ScopeType.ROLES)


def containing_scopes(scope: Scope) -> list[Scope]:
    """The listings a vanished object behind `scope` would have to drop out of.

    A fetch that fails with "does not exist" refreshes these, so a node for
    something dropped elsewhere disappears right away instead of waiting out
    the TTL. One level up only: if that level is gone too, its own refresh
    fails the same way and climbs on from there.

    No default arm, same as `Scope.key`: a new scope type has to decide.
    """
    match scope.type:
        case ScopeType.DATABASES | ScopeType.WAREHOUSES | ScopeType.ROLES:
            return []
        case ScopeType.SCHEMAS | ScopeType.DATABASE_PRIME:
            return [databases_scope()]
        case ScopeType.GRANTS if scope.table is not None:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.object_type is not None
            return [objects_scope(scope.object_type, scope.database, scope.schema)]
        case ScopeType.GRANTS | ScopeType.TAG_REFERENCES if scope.schema is None:
            return [databases_scope()]
        case (
            ScopeType.OBJECTS
            | ScopeType.TABLE_STATS
            | ScopeType.VIEW_STATS
            | ScopeType.MATERIALIZED_VIEW_STATS
            | ScopeType.STAGE_STATS
            | ScopeType.PIPE_ACTIVITY
            | ScopeType.TASK_ACTIVITY
            | ScopeType.GRANTS
            | ScopeType.TAG_REFERENCES
        ):
            assert scope.database is not None
            return [schemas_scope(scope.database)]
        case ScopeType.COLUMNS | ScopeType.COLUMN_TAG_REFERENCES:
            assert scope.database is not None
            assert scope.schema is not None
            # The scope names a table or a view without saying which.
            return [
                objects_scope(ObjectType.TABLE, scope.database, scope.schema),
                objects_scope(ObjectType.VIEW, scope.database, scope.schema),
            ]
        case ScopeType.PIPE_HISTORY:
            # Its statement names the pipe's *target table*, and the pipe only
            # as a filter value, so "does not exist" is about the table: the
            # pipe list has nothing to drop. A table dropped or out of the
            # role's sight is a lasting state the pipe's History note
            # reports, not a stale listing.
            return []
        case ScopeType.PIPE_ACTIVITY_HISTORY | ScopeType.PIPE_STATUS:
            assert scope.database is not None
            assert scope.schema is not None
            return [objects_scope(ObjectType.PIPE, scope.database, scope.schema)]
        case ScopeType.TASK_HISTORY | ScopeType.TASK_ACTIVITY_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            return [objects_scope(ObjectType.TASK, scope.database, scope.schema)]
        case ScopeType.DATA_PREVIEW:
            assert scope.database is not None
            assert scope.schema is not None
            # The same: a preview is of a table or a view.
            return [
                objects_scope(ObjectType.TABLE, scope.database, scope.schema),
                objects_scope(ObjectType.VIEW, scope.database, scope.schema),
            ]


def due_in(age: float | None, ttl: float) -> float | None:
    """Seconds until cached rows `age` seconds old are due a refetch.

    Zero or less once they are, and None when they never will be because a
    `ttl` of 0 disables *re*-freshing. Never fetched (`age is None`) is due
    now, whatever the `ttl`: that only ever disables refreshing, never the
    first fetch.

    Every scope ages on the same `ttl`. One that is expensive to fetch in
    full is made cheap to refresh instead, never refreshed less often --
    see `repository.plan_window`.

    The sweep and the footer countdown read this through `Entry.due`, the
    paint-time checks through `is_stale`, so they always agree.
    """
    if age is None:
        return 0.0
    if ttl <= 0:
        return None
    return ttl - age


def is_stale(age: float | None, ttl: float) -> bool:
    """Whether cached rows `age` seconds old are due a refetch.

    The yes/no form of `due_in`, for the paint-time checks:
    `_ScopedView.ensure` (a detail view's) and `ObjectTree._ensure` (the
    tree's). They drifted apart once already -- the tree was checking a TTL
    snapshotted at construction -- which is why the decision lives in
    `due_in` rather than being written out in each.
    """
    remaining = due_in(age, ttl)
    return remaining is not None and remaining <= 0


@dataclass(frozen=True)
class Database:
    name: str


@dataclass(frozen=True)
class Warehouse:
    name: str
    size: str = ""
    is_default: bool = False


@dataclass(frozen=True)
class Role:
    name: str


@dataclass(frozen=True)
class Schema:
    database_name: str
    name: str


@dataclass(frozen=True)
class SFObject:
    """A row of any `SHOW <objects>` result, reduced to what the tree needs."""

    database_name: str
    schema_name: str
    name: str
    type: ObjectType


@dataclass(frozen=True)
class Column:
    column_name: str
    ordinal_position: int
    is_nullable: bool
    #: The full type as DESC TABLE prints it, e.g. NUMBER(38,0) where
    #: DATA_TYPE is just NUMBER -- what the column list shows.
    desc_type: str = ""
    default: str = ""
    comment: str = ""


@dataclass(frozen=True)
class ObjectRef:
    """What the tree and the search modal hand to the detail pane."""

    type: ObjectType
    database: str
    schema: str
    name: str

    @property
    def fqn(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}"


@dataclass(frozen=True)
class SchemaRef:
    """What the search modal hands back for a schema hit.

    Deliberately not an ObjectRef with some placeholder type: a schema has no
    ObjectType and no mirror row of its own to show, unlike a real object. It
    does have its own detail view (Grants/Tags, see SchemaSelected) -- but
    the tree gets there by revealing and expanding the node (see
    `ObjectTree.reveal_schema`) and letting the resulting highlight announce
    it, not by this ref carrying enough to open one directly the way an
    ObjectRef does.
    """

    database: str
    name: str
