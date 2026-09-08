"""Domain vocabulary: frozen dataclasses and enums.

Deliberately free of Textual and of snowflake.connector types, so widgets
never touch a driver object and every module here is testable without either
dependency installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ObjectKind(StrEnum):
    """A kind of Snowflake object that snowcone can list and show.

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


class ScopeKind(StrEnum):
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
    #: migration system. Deliberately carries no object_kind: a scope with
    #: both object_kind and database set gets silently upgraded to a
    #: whole-database SHOW-prime by MainScreen.on_scope_needed, which would
    #: fetch SHOW TABLES, not this SELECT -- columns_scope avoids the same
    #: trap the same way, for a table's own single-object fetch.
    TABLE_STATS = "table_stats"
    #: A schema's pipes' *latest* load each (bounded by the "pipe history
    #: fetch window" setting), sourced by a real SELECT against
    #: SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY rather than SHOW -- the second
    #: instance of the dual-source pattern TABLE_STATS established (see
    #: DESIGN.md). Scoped to (database, schema) like OBJECTS, but backed by
    #: its own mirror table. Deliberately carries no object_kind, for exactly
    #: the same reason TABLE_STATS does not -- see that comment. Its own data
    #: barely changes on the ordinary CACHE_TTL cadence, so it carries a much
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
    #: like PIPE_ACTIVITY, but backed by its own mirror table. Deliberately
    #: carries no object_kind, for the same reason PIPE_ACTIVITY does not.
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
    #: same as every plain SHOW scope. Deliberately carries no object_kind:
    #: a scope with both object_kind and database set gets silently upgraded
    #: to a whole-database SHOW-prime by MainScreen.on_scope_needed, which
    #: would be wrong here -- same trap TABLE_STATS/PIPE_ACTIVITY avoid.
    #: granted_to can be ROLE, DATABASE ROLE, USER, SHARE or APPLICATION
    #: ROLE -- deliberately not filtered down to roles only.
    GRANTS = "grants"
    #: Tags applied to one schema or one database (schema is None), sourced
    #: by a real SELECT against INFORMATION_SCHEMA.TAG_REFERENCES rather than
    #: SHOW -- the fourth instance of the dual-source pattern TABLE_STATS
    #: established (see DESIGN.md). Needs a running warehouse; degrades
    #: blank without one, same as every SELECT-backed scope. Deliberately
    #: carries no object_kind, for exactly the same reason GRANTS does not.
    TAG_REFERENCES = "tag_references"
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

    kind: ScopeKind
    database: str | None = None
    schema: str | None = None
    object_kind: ObjectKind | None = None
    table: str | None = None

    @property
    def key(self) -> str:
        """The stable string stored in `cache_entry.scope`.

        Chosen to be readable when eyeballing the sqlite file. It is treated
        as opaque -- nothing parses it back into a Scope, so a database or
        schema name containing a dot cannot cause an ambiguity.
        """
        match self.kind:
            case ScopeKind.DATABASES:
                return "databases"
            case ScopeKind.SCHEMAS:
                return f"schemas:{self.database}"
            case ScopeKind.OBJECTS:
                assert self.object_kind is not None
                return f"{self.object_kind.plural}:{self.database}.{self.schema}"
            case ScopeKind.COLUMNS:
                return f"columns:{self.database}.{self.schema}.{self.table}"
            case ScopeKind.DATABASE_PRIME:
                assert self.object_kind is not None
                return f"{self.object_kind.plural}@{self.database}"
            case ScopeKind.COLUMNS_PRIME:
                if self.schema is None:
                    return f"columns_prime:{self.database}"
                return f"columns_prime:{self.database}.{self.schema}"
            case ScopeKind.TABLE_STATS:
                return f"table_stats:{self.database}.{self.schema}"
            case ScopeKind.PIPE_ACTIVITY:
                return f"pipe_activity:{self.database}.{self.schema}"
            case ScopeKind.PIPE_ACTIVITY_HISTORY:
                return (
                    f"pipe_activity_history:{self.database}.{self.schema}.{self.table}"
                )
            case ScopeKind.PIPE_HISTORY:
                return f"pipe_history:{self.database}.{self.schema}.{self.table}"
            case ScopeKind.TASK_ACTIVITY:
                return f"task_activity:{self.database}.{self.schema}"
            case ScopeKind.TASK_ACTIVITY_HISTORY:
                return (
                    f"task_activity_history:{self.database}.{self.schema}.{self.table}"
                )
            case ScopeKind.TASK_HISTORY:
                return f"task_history:{self.database}.{self.schema}.{self.table}"
            case ScopeKind.GRANTS:
                if self.schema is None:
                    return f"grants:{self.database}"
                return f"grants:{self.database}.{self.schema}"
            case ScopeKind.TAG_REFERENCES:
                if self.schema is None:
                    return f"tag_references:{self.database}"
                return f"tag_references:{self.database}.{self.schema}"
            case ScopeKind.WAREHOUSES:
                return "warehouses"
            case ScopeKind.ROLES:
                return "roles"

    def __str__(self) -> str:
        return self.key


def databases_scope() -> Scope:
    return Scope(ScopeKind.DATABASES)


def schemas_scope(database: str) -> Scope:
    return Scope(ScopeKind.SCHEMAS, database=database)


def objects_scope(kind: ObjectKind, database: str, schema: str) -> Scope:
    return Scope(ScopeKind.OBJECTS, database=database, schema=schema, object_kind=kind)


def columns_scope(database: str, schema: str, table: str) -> Scope:
    return Scope(ScopeKind.COLUMNS, database=database, schema=schema, table=table)


def prime_scope(kind: ObjectKind, database: str) -> Scope:
    return Scope(ScopeKind.DATABASE_PRIME, database=database, object_kind=kind)


def columns_prime_scope(database: str, schema: str | None = None) -> Scope:
    return Scope(ScopeKind.COLUMNS_PRIME, database=database, schema=schema)


def table_stats_scope(database: str, schema: str) -> Scope:
    # No object_kind -- see ScopeKind.TABLE_STATS's comment.
    return Scope(ScopeKind.TABLE_STATS, database=database, schema=schema)


def pipe_activity_scope(database: str, schema: str) -> Scope:
    # No object_kind -- see ScopeKind.PIPE_ACTIVITY's comment.
    return Scope(ScopeKind.PIPE_ACTIVITY, database=database, schema=schema)


def pipe_activity_history_scope(database: str, schema: str, pipe: str) -> Scope:
    # `table` holds the pipe's name -- same reuse `pipe_history_scope` makes.
    return Scope(
        ScopeKind.PIPE_ACTIVITY_HISTORY, database=database, schema=schema, table=pipe
    )


def pipe_history_scope(database: str, schema: str, pipe: str) -> Scope:
    # `table` holds the pipe's name here -- the generic "one object this
    # scope narrows to" slot, the same reuse `columns_scope` makes for a
    # table's name.
    return Scope(ScopeKind.PIPE_HISTORY, database=database, schema=schema, table=pipe)


def task_activity_scope(database: str, schema: str) -> Scope:
    # No object_kind -- see ScopeKind.TASK_ACTIVITY's comment.
    return Scope(ScopeKind.TASK_ACTIVITY, database=database, schema=schema)


def task_activity_history_scope(database: str, schema: str, task: str) -> Scope:
    # `table` holds the task's name -- same reuse `pipe_activity_history_scope`
    # makes for the pipe's name.
    return Scope(
        ScopeKind.TASK_ACTIVITY_HISTORY, database=database, schema=schema, table=task
    )


def task_history_scope(database: str, schema: str, task: str) -> Scope:
    # `table` holds the task's name here -- same reuse `pipe_history_scope`
    # makes for the pipe's name.
    return Scope(ScopeKind.TASK_HISTORY, database=database, schema=schema, table=task)


def grants_scope(database: str, schema: str | None = None) -> Scope:
    # No object_kind -- see ScopeKind.GRANTS's comment. schema=None means a
    # database-level grants scope, schema set means schema-level -- same
    # optional-schema convention COLUMNS_PRIME already uses.
    return Scope(ScopeKind.GRANTS, database=database, schema=schema)


def tag_references_scope(database: str, schema: str | None = None) -> Scope:
    # No object_kind -- see ScopeKind.TAG_REFERENCES's comment.
    return Scope(ScopeKind.TAG_REFERENCES, database=database, schema=schema)


def warehouses_scope() -> Scope:
    return Scope(ScopeKind.WAREHOUSES)


def roles_scope() -> Scope:
    return Scope(ScopeKind.ROLES)


#: Refresh cadence, in seconds, for a scope whose own data does not change on
#: the ordinary CACHE_TTL cadence -- see ScopeKind.PIPE_ACTIVITY's comment. A
#: plain constant, not a Setting: it is a correctness bound, not a
#: preference. 13 days, one short of INFORMATION_SCHEMA.COPY_HISTORY's 14-day
#: retention limit (`gateway.queries.PIPE_HISTORY_RETENTION_DAYS`) -- the two
#: sources must jointly cover every day with no gap between them, and
#: ACCOUNT_USAGE's own ingestion lag (up to a few hours) eats into a boundary
#: cut exactly at 14, so this stays one day inside it.
_TTL_OVERRIDES: dict[ScopeKind, float] = {
    ScopeKind.PIPE_ACTIVITY: 13 * 86400,
    ScopeKind.PIPE_ACTIVITY_HISTORY: 13 * 86400,
    # 6 days, one short of INFORMATION_SCHEMA.TASK_HISTORY's 7-day retention
    # (`gateway.queries.TASK_HISTORY_RETENTION_DAYS`) -- same margin reasoning
    # as PIPE_ACTIVITY_HISTORY's 13/14 pair above. TASK_ACTIVITY deliberately
    # carries no override -- see its own comment on ScopeKind.TASK_ACTIVITY.
    ScopeKind.TASK_ACTIVITY_HISTORY: 6 * 86400,
}


def scope_ttl(scope: Scope, default: float) -> float:
    """The TTL that actually governs `scope`'s staleness.

    Almost every scope just uses the caller's TTL (the CACHE_TTL setting).
    The rare exception is a scope kind in `_TTL_OVERRIDES`, whose own data
    does not change on that cadence -- refreshing it that often would just
    repeat the same round trip for no new data. Never faster than the
    caller's own default, only ever slower -- an aggressive default TTL is
    still honoured for every scope that has no override.

    Called from both `Repository.stale_scopes` (the background sweep) and
    `_ScopedView.ensure` (a view's own paint-time check), so the override
    applies uniformly regardless of which path is asking.
    """
    override = _TTL_OVERRIDES.get(scope.kind)
    if override is None:
        return default
    return max(default, override)


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
    kind: ObjectKind
    comment: str = ""
    rows: int | None = None
    bytes: int | None = None


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

    kind: ObjectKind
    database: str
    schema: str
    name: str

    @property
    def fqn(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}"


@dataclass(frozen=True)
class SchemaRef:
    """What the search modal hands back for a schema hit.

    Deliberately not an ObjectRef with some placeholder kind: a schema has no
    ObjectKind and no mirror row of its own to show, unlike a real object. It
    does have its own detail view (Grants/Tags, see SchemaSelected) -- but
    the tree gets there by revealing and expanding the node (see
    `ObjectTree.reveal_schema`) and letting the resulting highlight announce
    it, not by this ref carrying enough to open one directly the way an
    ObjectRef does.
    """

    database: str
    name: str
