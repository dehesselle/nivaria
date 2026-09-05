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

import json
from collections.abc import Sequence
from typing import Any

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


def show_objects_in_database(
    kind: ObjectKind,
    database: str,
    *,
    after: str | None = None,
    limit: int = 1000,
) -> str:
    """Every object of a kind in a whole database, in one statement.

    Measured against a live account: this returned 2145 tables across 54
    schemas in about a second, where visiting those schemas one at a time
    would have cost 54 round trips at ~187ms each. Round trips dominate
    completely -- payload is nearly free -- so priming a database this way is
    an order of magnitude cheaper than filling it schema by schema.
    """
    scope = quote_identifier(database)
    return f"SHOW {kind.plural.upper()} IN DATABASE {scope}{_paging(after, limit)}"


def show_columns(database: str, schema: str, table: str) -> str:
    """A table's columns without needing a running warehouse.

    INFORMATION_SCHEMA.COLUMNS is an ordinary SELECT and so needs a warehouse;
    SHOW COLUMNS is served by the metadata layer like every other SHOW. That
    matters because a session may legitimately have no warehouse at all -- the
    account this was verified against does not.
    """
    target = f"{quote_identifier(database)}.{quote_identifier(schema)}.{quote_identifier(table)}"
    return f"SHOW COLUMNS IN TABLE {target}"


def _data_type_fields(raw: object) -> dict[str, object]:
    """Unpack SHOW COLUMNS' data_type, which is a JSON string.

    e.g. {"type":"TEXT","length":16777216,"nullable":true} or
    {"type":"FIXED","precision":38,"scale":0,"nullable":true}. Malformed or
    unexpected content degrades to "type unknown" rather than failing the
    whole fetch: a column snowcone cannot describe is still a column worth
    listing.
    """
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def map_show_columns(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map SHOW COLUMNS output onto the INFORMATION_SCHEMA.COLUMNS shape.

    The mirror keeps INFORMATION_SCHEMA's vocabulary because that is the
    documented, stable name for each of these fields; only the source changed.

    SHOW COLUMNS has no ORDINAL_POSITION -- it returns columns in declaration
    order instead -- so position is derived from that order, counted per table
    so a multi-table result does not keep incrementing across tables.
    """
    position: dict[tuple[str, str, str], int] = {}
    mapped: list[dict[str, Any]] = []
    for row in rows:
        catalog = str(row.get("database_name", ""))
        schema = str(row.get("schema_name", ""))
        table = str(row.get("table_name", ""))
        key = (catalog, schema, table)
        position[key] = position.get(key, 0) + 1

        fields = _data_type_fields(row.get("data_type"))
        # The SHOW column is literally named "null?"; fall back to the parsed
        # payload's own nullable flag when it is absent.
        nullable = row.get("null?")
        if nullable is None:
            nullable = "Y" if fields.get("nullable", True) else "N"

        mapped.append(
            {
                "TABLE_CATALOG": catalog,
                "TABLE_SCHEMA": schema,
                "TABLE_NAME": table,
                "COLUMN_NAME": row.get("column_name"),
                "ORDINAL_POSITION": position[key],
                "DATA_TYPE": fields.get("type", "UNKNOWN"),
                "IS_NULLABLE": "YES"
                if str(nullable).upper() in {"Y", "YES", "TRUE"}
                else "NO",
                "COLUMN_DEFAULT": row.get("default"),
                "CHARACTER_MAXIMUM_LENGTH": fields.get("length"),
                "NUMERIC_PRECISION": fields.get("precision"),
                "NUMERIC_SCALE": fields.get("scale"),
                "COMMENT": row.get("comment"),
            }
        )
    return mapped
