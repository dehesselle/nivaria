"""Snowflake statement builders.

Every statement snowcone sends is built here and nowhere else, so the paging
strategy can be swapped in one place if `LIMIT ... FROM` turns out to be
unavailable for some object kind.

Two different mechanisms are used for injecting values, deliberately:

* Identifiers (database / schema names) cannot be bound as parameters, so they
  are double-quoted with `quote_identifier`. Quoting also preserves the case
  of a lower-case or mixed-case identifier, which an unquoted name would not.
* Ordinary values (the `FROM 'name'` paging cursor, INFORMATION_SCHEMA
  predicates) are bound with the driver's pyformat parameters where the
  statement allows it, and quoted with `quote_literal` where it does not --
  `SHOW ... FROM` takes a literal, not a bind.
"""

from __future__ import annotations

from snowcone.models import ObjectKind

#: Snowflake caps a SHOW result at 10k rows regardless of what LIMIT asks for.
MAX_PAGE_SIZE = 10_000


def quote_identifier(name: str) -> str:
    """Double-quote an identifier, escaping embedded quotes."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def quote_literal(value: str) -> str:
    """Single-quote a string literal, escaping embedded quotes."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _paging(after: str | None, limit: int) -> str:
    """The `LIMIT n [FROM 'name']` keyset-pagination tail shared by SHOW.

    Keyset paging on the object name, rather than bucketing names by first
    letter: bucket sizes in a real warehouse are wildly uneven (a large share
    of tables start with STG_/DIM_/FACT_), whereas every keyset page is
    bounded work by construction.
    """
    clamped = max(1, min(limit, MAX_PAGE_SIZE))
    tail = f" LIMIT {clamped}"
    if after is not None:
        tail += f" FROM {quote_literal(after)}"
    return tail


def show_databases(*, after: str | None = None, limit: int = 1000) -> str:
    return f"SHOW DATABASES{_paging(after, limit)}"


def show_schemas(database: str, *, after: str | None = None, limit: int = 1000) -> str:
    return (
        f"SHOW SCHEMAS IN DATABASE {quote_identifier(database)}{_paging(after, limit)}"
    )


def show_objects(
    kind: ObjectKind,
    database: str,
    schema: str,
    *,
    after: str | None = None,
    limit: int = 1000,
) -> str:
    scope = f"{quote_identifier(database)}.{quote_identifier(schema)}"
    return f"SHOW {kind.plural.upper()} IN SCHEMA {scope}{_paging(after, limit)}"


#: The INFORMATION_SCHEMA.COLUMNS columns snowcone mirrors. Named and ordered
#: exactly as Snowflake returns them so the cache table is a faithful mirror.
COLUMN_FIELDS = (
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
)


def information_schema_columns(
    database: str,
    schema: str,
    table: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Return (statement, params) for a table's -- or a whole schema's -- columns.

    Unlike SHOW, this needs a running warehouse, which is why it is only used
    on demand for a detail view rather than for the tree.
    """
    fields = ", ".join(COLUMN_FIELDS)
    statement = (
        f"SELECT {fields} FROM {quote_identifier(database)}.INFORMATION_SCHEMA.COLUMNS"
        " WHERE TABLE_SCHEMA = %s"
    )
    params: tuple[str, ...] = (schema,)
    if table is not None:
        statement += " AND TABLE_NAME = %s"
        params += (table,)
    statement += " ORDER BY TABLE_NAME, ORDINAL_POSITION"
    return statement, params
