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


def show_warehouses() -> str:
    """Every warehouse in one call.

    Unlike the object lists above, SHOW WAREHOUSES does not document LIMIT/FROM
    support, and an account's warehouse count is small enough that one unpaged
    call is always the whole picture.
    """
    return "SHOW WAREHOUSES"


def show_roles() -> str:
    """Every role in one call, for the same reason as `show_warehouses`."""
    return "SHOW ROLES"


def use_role(role: str) -> str:
    """Switch the session's active role.

    Issued once per pooled connection when the user picks a role from the
    role dialog -- see `ConnectionPool.set_role`.
    """
    return f"USE ROLE {quote_identifier(role)}"


def use_warehouse(warehouse: str) -> str:
    """Switch the session's active warehouse. See `use_role`."""
    return f"USE WAREHOUSE {quote_identifier(warehouse)}"


def show_columns(database: str, schema: str, table: str) -> str:
    """A table's columns without needing a running warehouse.

    INFORMATION_SCHEMA.COLUMNS is an ordinary SELECT and so needs a warehouse;
    SHOW COLUMNS is served by the metadata layer like every other SHOW. That
    matters because a session may legitimately have no warehouse at all -- the
    account this was verified against does not.
    """
    target = f"{quote_identifier(database)}.{quote_identifier(schema)}.{quote_identifier(table)}"
    return f"SHOW COLUMNS IN TABLE {target}"


def show_columns_in_database(
    database: str, *, after: str | None = None, limit: int = 1000
) -> str:
    """Every column of every table/view in a database, in one statement.

    Mirrors `show_objects_in_database`'s rationale: SHOW is metadata-layer, so
    this needs no warehouse either, and round trips dominate over payload, so
    one call beats one per table whenever the result fits under Snowflake's
    10k SHOW cap. Unlike `show_objects_in_database`, this is unverified against
    a live account -- SHOW COLUMNS IN DATABASE was not available to check
    against the reference account this codebase was otherwise verified on.
    """
    scope = quote_identifier(database)
    return f"SHOW COLUMNS IN DATABASE {scope}{_paging(after, limit)}"


def show_columns_in_schema(
    database: str, schema: str, *, after: str | None = None, limit: int = 1000
) -> str:
    """Every column of every table/view in one schema, in one statement.

    The fallback when `show_columns_in_database` is truncated -- see there.
    Also unverified against a live account, same caveat.
    """
    target = f"{quote_identifier(database)}.{quote_identifier(schema)}"
    return f"SHOW COLUMNS IN SCHEMA {target}{_paging(after, limit)}"


def table_stats(database: str) -> str:
    """LAST_DDL/LAST_ALTERED for every base table in a schema.

    An ordinary SELECT against INFORMATION_SCHEMA.TABLES -- unlike every
    builder above, this needs a running warehouse (see DESIGN.md's
    dual-source note). TABLE_SCHEMA and TABLE_TYPE are left as `%s`
    placeholders for the caller to bind rather than interpolated: unlike
    `SHOW ... FROM`, an ordinary SELECT accepts the driver's pyformat bind
    parameters, better injection hygiene than `quote_literal`. Only the
    database needs `quote_identifier` -- an identifier can't be bound.
    """
    target = f"{quote_identifier(database)}.INFORMATION_SCHEMA.TABLES"
    return (
        "SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, LAST_DDL, LAST_ALTERED "
        f"FROM {target} WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = %s"
    )


def show_grants_on_schema(database: str, schema: str) -> str:
    """Every grant on one schema -- metadata layer, needs no warehouse.

    Unpaged, like `show_warehouses`/`show_roles`: one schema's grant list is
    a small, bounded result, not the kind of thing that needs `LIMIT`/`FROM`
    keyset paging the object lists do.
    """
    target = f"{quote_identifier(database)}.{quote_identifier(schema)}"
    return f"SHOW GRANTS ON SCHEMA {target}"


def show_grants_on_database(database: str) -> str:
    """Every grant on one database. See `show_grants_on_schema`."""
    return f"SHOW GRANTS ON DATABASE {quote_identifier(database)}"


def tag_references(database: str) -> str:
    """Tags applied to one schema or one database.

    An ordinary SELECT against INFORMATION_SCHEMA.TAG_REFERENCES -- unlike
    `show_grants_on_schema`/`show_grants_on_database` above, this needs a
    running warehouse (see DESIGN.md's dual-source note). The object name
    and domain ('SCHEMA'/'DATABASE') bind as ordinary `%s` parameters, same
    as `table_stats`'s WHERE clause -- only the containing database (needed
    to address `db.INFORMATION_SCHEMA.TAG_REFERENCES` itself) is an
    identifier and needs `quote_identifier`.

    Deliberately not `SHOW TAGS IN SCHEMA`/`IN DATABASE`, which lists tag
    *objects defined inside* that container, not tags *applied to* it -- the
    wrong semantics for "what does this schema/database carry."
    """
    target = f"{quote_identifier(database)}.INFORMATION_SCHEMA.TAG_REFERENCES"
    return (
        "SELECT TAG_DATABASE, TAG_SCHEMA, TAG_NAME, TAG_VALUE, DOMAIN "
        f"FROM TABLE({target}(%s, %s))"
    )


def pipe_activity(fetch_days: int) -> str:
    """Every pipe's *latest* load in a schema, back to `fetch_days` ago.

    SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY, filtered to one schema's pipes and
    reduced to one row per pipe with QUALIFY -- this backs the "Pipes (n)"
    list (which only ever wants each pipe's most recent load) and
    `Repository.pipe_target_table`'s resolution, neither of which needs the
    full history. Bounding this to one row per pipe, in SQL, is the whole
    point: without it, a busy schema with hundreds of pipes and frequent
    ingestion returns unboundedly many historical rows for a query that only
    gets collapsed back down to one row per pipe in Python anyway (see
    `pipe_list_view.py`) -- the first version of this query did exactly that,
    and hung for minutes against a live account with ~400 pipes. A single
    pipe's full history (for its own detail view) is `pipe_activity_history`
    below instead -- lazy, per pipe, not part of this query.

    `fetch_days` comes from the "pipe history fetch window" setting, read
    fresh at fetch time. Like `table_stats`, this needs a running warehouse;
    unlike it, ACCOUNT_USAGE.COPY_HISTORY is a fixed name rather than one
    built from the database, so both filter values bind as ordinary `%s`
    parameters -- no `quote_identifier` needed at all. It also needs
    IMPORTED PRIVILEGES on the SNOWFLAKE database (or ACCOUNTADMIN); either
    gap just fails this one scope, same as a missing warehouse does for
    `table_stats`.
    """
    return (
        "SELECT PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME, FILE_NAME, "
        "FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, TABLE_CATALOG_NAME, "
        "TABLE_SCHEMA_NAME, TABLE_NAME, FIRST_ERROR_MESSAGE, "
        "FIRST_ERROR_LINE_NUMBER "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY "
        "WHERE PIPE_CATALOG_NAME = %s AND PIPE_SCHEMA_NAME = %s "
        f"AND LAST_LOAD_TIME >= DATEADD('day', -{int(fetch_days)}, CURRENT_TIMESTAMP()) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY PIPE_NAME ORDER BY LAST_LOAD_TIME DESC) = 1"
    )


def pipe_activity_history(fetch_days: int) -> str:
    """One pipe's full ACCOUNT_USAGE load history, back to `fetch_days` ago.

    The backfill half of a single pipe's detail view -- `pipe_activity`
    above deliberately only keeps the latest row per pipe, so a pipe's own
    "Load history" tab needs this instead, scoped to just that one pipe and
    fetched lazily only once that pipe's view is opened (`PipeView`), never
    eagerly for a whole schema. Unlike `pipe_history` (INFORMATION_SCHEMA's
    table function, which has no PIPE_NAME argument and must resolve a
    target table instead), ACCOUNT_USAGE.COPY_HISTORY already carries
    PIPE_NAME as an ordinary column, so this needs no target-table
    resolution at all -- just one more bound `%s` alongside `pipe_activity`'s
    two.
    """
    return (
        "SELECT PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME, FILE_NAME, "
        "FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, TABLE_CATALOG_NAME, "
        "TABLE_SCHEMA_NAME, TABLE_NAME, FIRST_ERROR_MESSAGE, "
        "FIRST_ERROR_LINE_NUMBER "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY "
        "WHERE PIPE_CATALOG_NAME = %s AND PIPE_SCHEMA_NAME = %s AND PIPE_NAME = %s "
        f"AND LAST_LOAD_TIME >= DATEADD('day', -{int(fetch_days)}, CURRENT_TIMESTAMP())"
    )


#: Snowflake's own retention limit for INFORMATION_SCHEMA.COPY_HISTORY -- a
#: platform constant, not a preference, so it is not a Setting. This is also
#: why ScopeKind.PIPE_ACTIVITY's TTL override (models.py) stays one day
#: under this: the two sources must jointly cover every day with no gap.
PIPE_HISTORY_RETENTION_DAYS = 14


def pipe_history(target_catalog: str, target_schema: str, target_table: str) -> str:
    """One pipe's near-real-time load history, the last 14 days.

    INFORMATION_SCHEMA.COPY_HISTORY, a table function that requires a
    TABLE_NAME argument -- Snowflake exposes no way to ask for "this pipe's"
    history directly, so the caller resolves the pipe's target table first
    (from the already-cached ACCOUNT_USAGE.COPY_HISTORY data -- see
    FetchService._resolve_pipe_target) and this just filters that table's
    history down to one pipe.

    TABLE_NAME and START_TIME are table-function named arguments, not an
    ordinary SELECT's WHERE clause -- whether the driver's pyformat bind
    works inside one is untested, so both are quote_literal'd/computed as
    plain SQL text instead, the same "where binding does not clearly apply"
    reasoning `queries.py`'s own module docstring already carries for
    `SHOW ... FROM`. PIPE_NAME, filtering the function's *output*, is an
    ordinary WHERE value and binds normally.
    """
    target = f"{quote_identifier(target_catalog)}.INFORMATION_SCHEMA.COPY_HISTORY"
    qualified_table = quote_literal(f"{target_catalog}.{target_schema}.{target_table}")
    return (
        "SELECT FILE_NAME, FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, PIPE_NAME, "
        "FIRST_ERROR_MESSAGE, FIRST_ERROR_LINE_NUMBER "
        f"FROM TABLE({target}("
        f"TABLE_NAME => {qualified_table}, "
        "START_TIME => DATEADD('day', "
        f"-{PIPE_HISTORY_RETENTION_DAYS}, CURRENT_TIMESTAMP())"
        ")) "
        "WHERE PIPE_NAME = %s"
    )


def task_activity(fetch_days: int) -> str:
    """Every task's *latest* run in a schema, back to `fetch_days` ago.

    SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY, filtered to one schema's tasks and
    reduced to one row per task with QUALIFY -- the same shape `pipe_activity`
    uses, and for the same reason: a schema full of frequently-running tasks
    could return unboundedly many historical rows for a query that only ever
    gets collapsed back down to one row per task in Python anyway (see
    `task_list_view.py`). A single task's full history (for its own detail
    view) is `task_activity_history` below instead -- lazy, per task, not
    part of this query.

    `fetch_days` comes from the "task history fetch window" setting, read
    fresh at fetch time. Like `pipe_activity`, this needs a running warehouse
    (unverified -- see DESIGN.md) and IMPORTED PRIVILEGES on the SNOWFLAKE
    database (or ACCOUNTADMIN); either gap just fails this one scope, same as
    a missing warehouse does for `table_stats`.
    """
    return (
        "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, QUERY_ID, SCHEDULED_TIME, "
        "QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, ERROR_MESSAGE "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
        "WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s "
        f"AND SCHEDULED_TIME >= DATEADD('day', -{int(fetch_days)}, CURRENT_TIMESTAMP()) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY NAME ORDER BY SCHEDULED_TIME DESC) = 1"
    )


def task_activity_history(fetch_days: int) -> str:
    """One task's full ACCOUNT_USAGE run history, back to `fetch_days` ago.

    The backfill half of a single task's detail view -- `task_activity` above
    deliberately only keeps the latest row per task, so a task's own "Run
    history" tab needs this instead, scoped to just that one task and fetched
    lazily only once that task's view is opened (`TaskView`), never eagerly
    for a whole schema. Like `pipe_activity_history`, this needs no
    target-table resolution: ACCOUNT_USAGE.TASK_HISTORY already carries NAME
    as an ordinary column, so this is just one more bound `%s` alongside
    `task_activity`'s two.
    """
    return (
        "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, QUERY_ID, SCHEDULED_TIME, "
        "QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, ERROR_MESSAGE "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
        "WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s AND NAME = %s "
        f"AND SCHEDULED_TIME >= DATEADD('day', -{int(fetch_days)}, CURRENT_TIMESTAMP())"
    )


#: Snowflake's own hard retention limit for INFORMATION_SCHEMA.TASK_HISTORY --
#: a platform constant, not a preference, so it is not a Setting. Shorter than
#: PIPE_HISTORY_RETENTION_DAYS' 14 days. This is also why
#: ScopeKind.TASK_ACTIVITY_HISTORY's TTL override (models.py) stays one day
#: under this: the two sources must jointly cover every day with no gap.
TASK_HISTORY_RETENTION_DAYS = 7


def task_history(database: str, schema: str, task: str) -> str:
    """One task's near-real-time run history, the last 7 days.

    INFORMATION_SCHEMA.TASK_HISTORY, a table function that -- unlike
    INFORMATION_SCHEMA.COPY_HISTORY -- accepts TASK_NAME/DATABASE_NAME/
    SCHEMA_NAME directly as named arguments, all of which a task's own Scope
    already carries. So unlike `pipe_history`, this needs no side-channel
    target resolution before it can be called at all.

    The function name itself is still database-qualified
    (`quote_identifier(database).INFORMATION_SCHEMA.TASK_HISTORY`), same as
    `pipe_history` qualifies COPY_HISTORY -- confirmed against a live account:
    an unqualified `INFORMATION_SCHEMA.TASK_HISTORY` resolves against whatever
    database happens to be current in the session, which the connection pool
    never sets (only USE ROLE/USE WAREHOUSE are ever issued -- see "The
    connection pool" in DESIGN.md), so it fails with "invalid identifier
    INFORMATION_SCHEMA.TASK_HISTORY" regardless of which database the task
    actually lives in. The DATABASE_NAME/SCHEMA_NAME named arguments below are
    a separate thing -- they filter the function's *output*, not its own
    name resolution.

    Every named argument here is a table-function argument, not an ordinary
    SELECT's WHERE clause -- whether the driver's pyformat bind works inside
    one is untested, so all of them are quote_literal'd/computed as plain SQL
    text instead, the same reasoning `pipe_history` already documents. There
    is therefore no bound `%s` parameter at all here, unlike `pipe_history`,
    where PIPE_NAME filters the function's *output* rather than being a named
    argument itself.

    RESULT_LIMIT is set to the function's own maximum (10000) to avoid
    truncation within the 7-day window -- a task history light enough to fit
    on screen is never anywhere near that many rows in practice.
    """
    target = f"{quote_identifier(database)}.INFORMATION_SCHEMA.TASK_HISTORY"
    return (
        "SELECT NAME, QUERY_ID, SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME, "
        "STATE, ERROR_CODE, ERROR_MESSAGE "
        f"FROM TABLE({target}("
        f"DATABASE_NAME => {quote_literal(database)}, "
        f"SCHEMA_NAME => {quote_literal(schema)}, "
        f"TASK_NAME => {quote_literal(task)}, "
        "RESULT_LIMIT => 10000, "
        "SCHEDULED_TIME_RANGE_START => DATEADD('day', "
        f"-{TASK_HISTORY_RETENTION_DAYS}, CURRENT_TIMESTAMP())"
        "))"
    )


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

        category = fields.get("type", "UNKNOWN")
        # The JSON blob reports the same "precision"/"scale" keys for FIXED's
        # decimal scale and for a timestamp/time's fractional-seconds count;
        # INFORMATION_SCHEMA keeps those concepts in separate columns, so the
        # mirror gates each source key to the category it actually describes.
        is_datetime = category in {
            "TIMESTAMP_LTZ",
            "TIMESTAMP_NTZ",
            "TIMESTAMP_TZ",
            "TIME",
        }

        mapped.append(
            {
                "TABLE_CATALOG": catalog,
                "TABLE_SCHEMA": schema,
                "TABLE_NAME": table,
                "COLUMN_NAME": row.get("column_name"),
                "ORDINAL_POSITION": position[key],
                "DATA_TYPE": category,
                "IS_NULLABLE": "YES"
                if str(nullable).upper() in {"Y", "YES", "TRUE"}
                else "NO",
                "COLUMN_DEFAULT": row.get("default"),
                "CHARACTER_MAXIMUM_LENGTH": fields.get("length"),
                "NUMERIC_PRECISION": fields.get("precision")
                if category == "FIXED"
                else None,
                "NUMERIC_SCALE": fields.get("scale") if category == "FIXED" else None,
                "DATETIME_PRECISION": fields.get("scale") if is_datetime else None,
                "COMMENT": row.get("comment"),
            }
        )
    return mapped
