"""Domain vocabulary: frozen dataclasses and enums.

Deliberately free of Textual and of snowflake.connector types, so widgets
never touch a driver object and every module here is testable without either
dependency installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ObjectType(StrEnum):
    """A type of Snowflake object that snowcone can list and show.

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
        row in the schema or database regardless -- which made the keyset
        continuation in `FetchService._execute` non-terminating, since every
        "next page" came back identical to the last. Declared here rather than
        discovered at runtime, so the statement builder can stop asking for
        something that does not work instead of the scheduler having to
        recover from it. See DESIGN.md, "Bounding a result".
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
    #: Not a set of rows of its own, same as DATABASE_PRIME: a request to fill
    #: many tables' COLUMNS scopes from one statement, either for a whole
    #: database (schema is None) or for one schema's fallback chunk.
    COLUMNS_PRIME = "columns_prime"
    #: A schema's tables' LAST_DDL/LAST_ALTERED, sourced by a real SELECT
    #: against INFORMATION_SCHEMA.TABLES rather than SHOW -- the first
    #: deliberate exception to SHOW-only (see DESIGN.md). Scoped to
    #: (database, schema) like OBJECTS, but backed by its own mirror table
    #: rather than a retrofit onto show_tables, since schema.sql has no
    #: migration system.
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
    #: A schema's pipes' *latest* load each (bounded by the "pipe history
    #: fetch window" setting), sourced by a real SELECT against
    #: SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY rather than SHOW -- the second
    #: instance of the dual-source pattern TABLE_STATS established (see
    #: DESIGN.md). Scoped to (database, schema) like OBJECTS, but backed by
    #: its own mirror table. Its own data barely changes on the ordinary CACHE_TTL cadence, so it carries a much
    #: longer effective TTL -- see `scope_ttl` below. Reduced to one row per
    #: pipe in SQL (see queries.pipe_activity) -- a single pipe's own full
    #: history is PIPE_ACTIVITY_HISTORY below, not this scope.
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
    #: Every task's *latest* run in a schema (bounded by the "task history
    #: fetch window" setting), sourced by a real SELECT against
    #: SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY rather than SHOW -- the same
    #: dual-source pattern PIPE_ACTIVITY established (see DESIGN.md), just
    #: against a different ACCOUNT_USAGE view. Scoped to (database, schema)
    #: like PIPE_ACTIVITY, but backed by its own mirror table.
    #: Reduced to one row per task in SQL (see queries.task_activity) -- a
    #: single task's own full run history is TASK_ACTIVITY_HISTORY below.
    #: Unlike PIPE_ACTIVITY, this carries no TTL override (see scope_ttl
    #: below): a task can run every few minutes, so a multi-day-stale "last
    #: run" column here would be actively misleading, not just questionable
    #: the way PIPE_ACTIVITY's own inherited override already admits it is.
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
    #: same as every plain SHOW scope.
    #: granted_to can be ROLE, DATABASE ROLE, USER, SHARE or APPLICATION
    #: ROLE -- deliberately not filtered down to roles only.
    GRANTS = "grants"
    #: Tags applied to one schema or one database (schema is None), sourced
    #: by a real SELECT against INFORMATION_SCHEMA.TAG_REFERENCES rather than
    #: SHOW -- the fourth instance of the dual-source pattern TABLE_STATS
    #: established (see DESIGN.md). Needs a running warehouse; degrades
    #: blank without one, same as every SELECT-backed scope.
    TAG_REFERENCES = "tag_references"
    #: One table's (or view's) columns' tags, sourced by a real SELECT
    #: against INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS -- a different
    #: function from TAG_REFERENCES above, purpose-built to return every tag
    #: on every column of one table in a single call rather than needing one
    #: TAG_REFERENCES call per column. Scoped to (database, schema, table)
    #: like COLUMNS. Needs a running warehouse; degrades blank without one,
    #: same as TAG_REFERENCES.
    COLUMN_TAG_REFERENCES = "column_tag_references"
    WAREHOUSES = "warehouses"
    ROLES = "roles"


class CacheState(StrEnum):
    EMPTY = "empty"
    PARTIAL = "partial"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass(frozen=True, order=True)
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
    #: Which object type an OBJECTS listing or a DATABASE_PRIME is for. Every
    #: other scope type leaves it unset -- they narrow by database/schema/
    #: table instead, and nothing keys off this field except those two.
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
            case ScopeType.COLUMNS_PRIME:
                if self.schema is None:
                    return f"columns_prime:{self.database}"
                return f"columns_prime:{self.database}.{self.schema}"
            case ScopeType.TABLE_STATS:
                return f"table_stats:{self.database}.{self.schema}"
            case ScopeType.VIEW_STATS:
                return f"view_stats:{self.database}.{self.schema}"
            case ScopeType.MATERIALIZED_VIEW_STATS:
                return f"materialized_view_stats:{self.database}.{self.schema}"
            case ScopeType.PIPE_ACTIVITY:
                return f"pipe_activity:{self.database}.{self.schema}"
            case ScopeType.PIPE_ACTIVITY_HISTORY:
                return (
                    f"pipe_activity_history:{self.database}.{self.schema}.{self.table}"
                )
            case ScopeType.PIPE_HISTORY:
                return f"pipe_history:{self.database}.{self.schema}.{self.table}"
            case ScopeType.TASK_ACTIVITY:
                return f"task_activity:{self.database}.{self.schema}"
            case ScopeType.TASK_ACTIVITY_HISTORY:
                return (
                    f"task_activity_history:{self.database}.{self.schema}.{self.table}"
                )
            case ScopeType.TASK_HISTORY:
                return f"task_history:{self.database}.{self.schema}.{self.table}"
            case ScopeType.GRANTS:
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


def columns_prime_scope(database: str, schema: str | None = None) -> Scope:
    return Scope(ScopeType.COLUMNS_PRIME, database=database, schema=schema)


def table_stats_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.TABLE_STATS, database=database, schema=schema)


def view_stats_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.VIEW_STATS, database=database, schema=schema)


def materialized_view_stats_scope(database: str, schema: str) -> Scope:
    return Scope(ScopeType.MATERIALIZED_VIEW_STATS, database=database, schema=schema)


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
    # schema-level -- the same optional-schema convention COLUMNS_PRIME uses.
    return Scope(ScopeType.GRANTS, database=database, schema=schema)


def tag_references_scope(database: str, schema: str | None = None) -> Scope:
    return Scope(ScopeType.TAG_REFERENCES, database=database, schema=schema)


def column_tag_references_scope(database: str, schema: str, table: str) -> Scope:
    return Scope(
        ScopeType.COLUMN_TAG_REFERENCES, database=database, schema=schema, table=table
    )


def warehouses_scope() -> Scope:
    return Scope(ScopeType.WAREHOUSES)


def roles_scope() -> Scope:
    return Scope(ScopeType.ROLES)


#: Refresh cadence, in seconds, for a scope whose own data does not change on
#: the ordinary CACHE_TTL cadence -- see ScopeType.PIPE_ACTIVITY's comment. A
#: plain constant, not a Setting: it is a correctness bound, not a
#: preference. 13 days, one short of INFORMATION_SCHEMA.COPY_HISTORY's 14-day
#: retention limit (`gateway.queries.PIPE_HISTORY_RETENTION_DAYS`) -- the two
#: sources must jointly cover every day with no gap between them, and
#: ACCOUNT_USAGE's own ingestion lag (up to a few hours) eats into a boundary
#: cut exactly at 14, so this stays one day inside it.
_TTL_OVERRIDES: dict[ScopeType, float] = {
    ScopeType.PIPE_ACTIVITY: 13 * 86400,
    ScopeType.PIPE_ACTIVITY_HISTORY: 13 * 86400,
    # 6 days, one short of INFORMATION_SCHEMA.TASK_HISTORY's 7-day retention
    # (`gateway.queries.TASK_HISTORY_RETENTION_DAYS`) -- same margin reasoning
    # as PIPE_ACTIVITY_HISTORY's 13/14 pair above. TASK_ACTIVITY deliberately
    # carries no override -- see its own comment on ScopeType.TASK_ACTIVITY.
    ScopeType.TASK_ACTIVITY_HISTORY: 6 * 86400,
}


def scope_ttl(scope: Scope, default: float) -> float:
    """The TTL that actually governs `scope`'s staleness.

    Almost every scope just uses the caller's TTL (the CACHE_TTL setting).
    The rare exception is a scope type in `_TTL_OVERRIDES`, whose own data
    does not change on that cadence -- refreshing it that often would just
    repeat the same round trip for no new data. Never faster than the
    caller's own default, only ever slower -- an aggressive default TTL is
    still honoured for every scope that has no override.

    Reached only through `is_stale` below, which is the single decision
    every staleness check in the app goes through, so the override applies
    uniformly regardless of which path is asking.
    """
    override = _TTL_OVERRIDES.get(scope.type)
    if override is None:
        return default
    return max(default, override)


def is_stale(scope: Scope, age: float | None, ttl: float) -> bool:
    """Whether cached rows `age` seconds old are due a refetch.

    The one place this is decided, called from all three that ask:
    `Repository.stale_scopes` (the background sweep), `_ScopedView.ensure`
    (a detail view's paint-time check) and `ObjectTree._ensure` (the tree's).
    They drifted apart once already -- the tree was checking a TTL
    snapshotted at construction and skipping `scope_ttl` entirely -- which is
    why the decision lives here rather than being written out three times.

    Never fetched (`age is None`) is always stale: it is maximally so, and a
    `ttl` of 0 only disables *re*-freshing, never the first fetch.
    """
    if age is None:
        return True
    effective = scope_ttl(scope, ttl) if ttl > 0 else ttl
    return effective > 0 and age >= effective


@dataclass(frozen=True)
class Database:
    name: str
    comment: str = ""


@dataclass(frozen=True)
class Warehouse:
    name: str
    comment: str = ""
    size: str = ""
    is_default: bool = False


@dataclass(frozen=True)
class Role:
    name: str
    comment: str = ""


@dataclass(frozen=True)
class Schema:
    database_name: str
    name: str
    comment: str = ""


@dataclass(frozen=True)
class SFObject:
    """A row of any `SHOW <objects>` result, reduced to what the tree needs."""

    database_name: str
    schema_name: str
    name: str
    type: ObjectType
    comment: str = ""


@dataclass(frozen=True)
class Column:
    table_catalog: str
    table_schema: str
    table_name: str
    column_name: str
    ordinal_position: int
    data_type: str
    is_nullable: bool
    character_maximum_length: int | None = None
    numeric_precision: int | None = None
    numeric_scale: int | None = None
    datetime_precision: int | None = None
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
