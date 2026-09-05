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


def warehouses_scope() -> Scope:
    return Scope(ScopeKind.WAREHOUSES)


def roles_scope() -> Scope:
    return Scope(ScopeKind.ROLES)


@dataclass(frozen=True)
class Database:
    name: str
    comment: str = ""


@dataclass(frozen=True)
class Warehouse:
    name: str
    comment: str = ""


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
    default: str = ""
    comment: str = ""


@dataclass(frozen=True)
class ObjectRef:
    """What the tree and the search bar hand to the detail pane."""

    kind: ObjectKind
    database: str
    schema: str
    name: str

    @property
    def fqn(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}"
