# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Snowflake statement builders.

Every statement nivaria sends is built here and nowhere else, so what goes to
Snowflake can be read in one place.

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

from nivaria.models import ObjectType

#: The largest LIMIT worth asking a SHOW for. Snowflake caps most SHOW
#: results near this, but not universally -- a real SHOW COLUMNS IN
#: DATABASE returned 11511 rows in one response -- so this bounds what
#: nivaria asks for, and is not a ceiling anything may rely on. The
#: ceiling that is enforced is ROW_CEILING, applied in PoolGateway.
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
    object_type: ObjectType,
    database: str,
    schema: str,
    *,
    after: str | None = None,
    limit: int = 1000,
) -> str:
    """One object type in one schema, keyset-paged by `after`/`limit`.

    Paged only for the types that actually honour it (`ObjectType.pageable`).
    Pipes and stages return every row regardless of `LIMIT`, so asking for a
    page would put a clause in the log that Snowflake never applied, and the
    caller would page a result that never shrinks -- `jobs.fetch_page` treats
    an unpageable type as a single complete result for the same reason.
    """
    scope = f"{quote_identifier(database)}.{quote_identifier(schema)}"
    tail = _paging(after, limit) if object_type.pageable else ""
    return f"SHOW {object_type.plural.upper()} IN SCHEMA {scope}{tail}"


def show_objects_in_database(
    object_type: ObjectType,
    database: str,
    *,
    limit: int = 1000,
) -> str:
    """Every object of a type in a whole database, in one statement.

    Measured against a live account: this returned 2145 tables across 54
    schemas in about a second, where visiting those schemas one at a time
    would have cost 54 round trips at ~187ms each. Round trips dominate
    completely -- payload is nearly free -- so priming a database this way is
    an order of magnitude cheaper than filling it schema by schema.

    It takes no cursor: names repeat across schemas, so resuming after one
    would skip rows (docs/DESIGN.md "Database priming"). The limit is omitted
    for an unpageable type, same as `show_objects`.
    """
    scope = quote_identifier(database)
    tail = _paging(None, limit) if object_type.pageable else ""
    return f"SHOW {object_type.plural.upper()} IN DATABASE {scope}{tail}"


def show_materialized_views(database: str, schema: str, *, limit: int = 1000) -> str:
    """Row/byte stats for materialized views in a schema.

    SHOW VIEWS never returns rows/bytes, even for materialized views (see
    schema.sql's show_views) -- SHOW MATERIALIZED VIEWS is Snowflake's
    separate command for that, with its own column set that does. Metadata
    layer like every other SHOW builder here, so unlike `table_stats`/
    `view_stats` this needs no warehouse.
    """
    scope = f"{quote_identifier(database)}.{quote_identifier(schema)}"
    return f"SHOW MATERIALIZED VIEWS IN SCHEMA {scope}{_paging(None, limit)}"


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


def current_account_names() -> str:
    """The session's organization and account name, for Snowsight links and
    for showing the account.

    Asked once per connect (see `ConnectionPool._read_account_names`): the
    login response carries the resolved role and warehouse but not these.
    Context functions only, so it needs no warehouse and no privilege --
    verified on the sandbox account as `PUBLIC` with `CURRENT_WAREHOUSE()`
    reading `NULL`.
    """
    return "SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT_NAME()"


def show_columns(database: str, schema: str, table: str) -> str:
    """A table's columns without needing a running warehouse.

    INFORMATION_SCHEMA.COLUMNS is an ordinary SELECT and so needs a warehouse;
    SHOW COLUMNS is served by the metadata layer like every other SHOW. That
    matters because a session may legitimately have no warehouse at all -- the
    account this was verified against does not.
    """
    target = f"{quote_identifier(database)}.{quote_identifier(schema)}.{quote_identifier(table)}"
    return f"SHOW COLUMNS IN TABLE {target}"


def table_stats(database: str) -> str:
    """LAST_DDL/LAST_ALTERED for every base table in a schema.

    An ordinary SELECT against INFORMATION_SCHEMA.TABLES -- unlike every
    builder above, this needs a running warehouse (see docs/DESIGN.md
    "A second source"). TABLE_SCHEMA and TABLE_TYPE are left as `%s`
    placeholders for the caller to bind rather than interpolated: unlike
    `SHOW ... FROM`, an ordinary SELECT accepts the driver's pyformat bind
    parameters, better injection hygiene than `quote_literal`. Only the
    database needs `quote_identifier` -- an identifier can't be bound.

    Also the statement `view_stats` below binds against, with `TABLE_TYPE`
    bound to `"VIEW"` instead of `"BASE TABLE"` -- INFORMATION_SCHEMA.TABLES
    lists views too, with the same LAST_DDL/LAST_ALTERED columns.
    """
    target = f"{quote_identifier(database)}.INFORMATION_SCHEMA.TABLES"
    return (
        "SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, LAST_DDL, LAST_ALTERED "
        f"FROM {target} WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = %s"
    )


def view_stats(database: str) -> str:
    """LAST_DDL/LAST_ALTERED for every view in a schema.

    The exact same statement as `table_stats` -- TABLE_TYPE is a bound `%s`,
    not baked into the SQL text, so the two differ only in what the caller
    binds ("BASE TABLE" vs "VIEW"). Kept as its own function anyway, so
    `PoolGateway.view_stats` reads as its own capability rather than a caller
    silently repurposing `table_stats`.
    """
    return table_stats(database)


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


def show_grants_on_object(
    object_type: ObjectType, database: str, schema: str, name: str
) -> str:
    """Every grant on one object, e.g. `SHOW GRANTS ON TABLE`.

    There is no `IN SCHEMA` form to batch this the way the object lists are
    primed, so it is one round trip per object -- which is why the Table
    view only asks for it while its Grants tab is shown. Unpaged, see
    `show_grants_on_schema`.
    """
    target = ".".join(quote_identifier(part) for part in (database, schema, name))
    return f"SHOW GRANTS ON {object_type.value} {target}"


def data_preview(database: str, schema: str, table: str, max_rows: int) -> str:
    """Some of one table's or view's rows, for its Data tab.

    No ORDER BY, so which rows come back is up to Snowflake. That is the
    mistake `task_history` documents for a history, where an arbitrary slice
    passes for the newest rows. Here no order is promised, and the Data tab
    says so. Ordering would make Snowflake scan and sort the whole table to
    hand back `max_rows` of it.
    """
    target = ".".join(quote_identifier(part) for part in (database, schema, table))
    return f"SELECT * FROM {target} LIMIT {int(max_rows)}"


def tag_references(database: str) -> str:
    """Tags applied to one schema or one database.

    An ordinary SELECT against INFORMATION_SCHEMA.TAG_REFERENCES -- unlike
    `show_grants_on_schema`/`show_grants_on_database` above, this needs a
    running warehouse (see docs/DESIGN.md "A second source"). The object name
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


def tag_references_all_columns(database: str) -> str:
    """Tags applied to every column of one table (or view).

    `INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS`, not `TAG_REFERENCES`
    above called once per column: this function returns every tag on every
    column of one table in a single call, so a table with a hundred columns
    still costs one round trip, not a hundred -- the same "no query whose
    cost scales with row count" discipline every other second-source scope
    here already follows. The object name binds as a single `%s`, the fully
    qualified `database.schema.table`; the domain argument is always the
    literal 'table' -- Snowflake's own function requires that even when the
    object is a view, so unlike `tag_references` there is no second bind
    parameter for it.

    The `WHERE` clause is load-bearing, confirmed against a real account:
    this function reports a tag set on the *schema or database* against
    every column underneath it too (`APPLY_METHOD = 'INHERITED'`),
    indistinguishable from a tag actually set on that column without this
    filter -- without it, every column of every table in a tagged schema
    shows that schema's tags as if they were its own. `IS DISTINCT FROM`
    rather than `!=` so legacy rows, where `APPLY_METHOD` is `NULL`, aren't
    silently dropped along with the inherited ones.
    """
    target = (
        f"{quote_identifier(database)}.INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS"
    )
    return (
        "SELECT TAG_DATABASE, TAG_SCHEMA, TAG_NAME, TAG_VALUE, COLUMN_NAME "
        f"FROM TABLE({target}(%s, 'table')) "
        "WHERE APPLY_METHOD IS DISTINCT FROM 'INHERITED'"
    )


def pipe_activity(window_seconds: int) -> str:
    """Every pipe's *latest* load in a schema, back to `window_seconds` ago.

    SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY, filtered to one schema's pipes and
    reduced to one row per pipe with QUALIFY -- this backs the "Pipes (n)"
    list (which only ever wants each pipe's most recent load) and
    `Repository.pipe_target_table`'s resolution, neither of which needs the
    full history. Bounding this to one row per pipe, in SQL, is the whole
    point: collapsing in Python instead means materialising every load of
    every pipe first, which hung for minutes against a live account with ~400
    pipes. A single pipe's full history (for its own detail view) is
    `pipe_activity_history` below instead -- lazy, per pipe, not part of this
    query.

    The window is the whole "pipe history fetch window" setting for a full
    pass, or the time since the last pass plus the lag margin for a window
    pass (`repository.plan_window`): the cost follows the window, not the
    rows, so a refresh asks for only a few hours. Like
    `table_stats`, this needs a running warehouse;
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
        f"AND LAST_LOAD_TIME >= {_since(window_seconds)} "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY PIPE_NAME ORDER BY LAST_LOAD_TIME DESC) = 1"
    )


def pipe_activity_history(window_seconds: int, max_rows: int) -> str:
    """One pipe's newest `max_rows` ACCOUNT_USAGE loads, back to `window_seconds` ago.

    The backfill half of a single pipe's detail view -- `pipe_activity`
    above deliberately only keeps the latest row per pipe, so a pipe's own
    History tab needs this instead, scoped to just that one pipe and
    fetched lazily only once that pipe's view is opened (`PipeView`), never
    eagerly for a whole schema. Unlike `pipe_history` (INFORMATION_SCHEMA's
    table function, which has no PIPE_NAME argument and must resolve a
    target table instead), ACCOUNT_USAGE.COPY_HISTORY already carries
    PIPE_NAME as an ordinary column, so this needs no target-table
    resolution at all -- just one more bound `%s` alongside `pipe_activity`'s
    two.

    One pipe is not by itself a small result. `pipe_activity`'s QUALIFY
    bounds the schema-wide query to one row per pipe, but nothing bounds a
    *single* pipe's history except the fetch window, and a pipe loading a
    file a minute across a 90-day window is six figures of rows. So the
    newest `max_rows` are taken, which is the half of the window the detail
    view shows anyway -- the ORDER BY is what makes that "the newest"
    rather than an arbitrary slice, and it is why this is a real bound and
    not just a smaller unbounded query.
    """
    return (
        "SELECT PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME, FILE_NAME, "
        "FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, PIPE_RECEIVED_TIME, "
        "TABLE_CATALOG_NAME, TABLE_SCHEMA_NAME, TABLE_NAME, FIRST_ERROR_MESSAGE, "
        "FIRST_ERROR_LINE_NUMBER "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY "
        "WHERE PIPE_CATALOG_NAME = %s AND PIPE_SCHEMA_NAME = %s AND PIPE_NAME = %s "
        f"AND LAST_LOAD_TIME >= {_since(window_seconds)} "
        f"ORDER BY LAST_LOAD_TIME DESC LIMIT {int(max_rows)}"
    )


#: Snowflake's own retention limit for INFORMATION_SCHEMA.COPY_HISTORY -- a
#: platform constant, not a preference, so it is not a Setting.
PIPE_HISTORY_RETENTION_DAYS = 14

#: How far behind ACCOUNT_USAGE.COPY_HISTORY can be, in seconds: Snowflake's
#: published "up to 2 hours", a platform figure rather than a preference.
#: The lag margin a window pass reaches back by is a multiple of it -- see
#: settings.LAG_MARGIN_FACTOR and repository.plan_window.
COPY_HISTORY_PUBLISHED_LAG = 2 * 3600

#: The same for ACCOUNT_USAGE.TASK_HISTORY: "up to 45 minutes".
TASK_HISTORY_PUBLISHED_LAG = 45 * 60


def _since(window_seconds: int) -> str:
    """The start of a window, relative to Snowflake's own clock.

    Relative rather than an absolute time sent from here, so a client
    clock that is off does not shift the window.
    """
    return f"DATEADD('second', -{int(window_seconds)}, CURRENT_TIMESTAMP())"


def pipe_history(
    target_catalog: str, target_schema: str, target_table: str, max_rows: int
) -> str:
    """One pipe's newest `max_rows` near-real-time loads, the last 14 days.

    INFORMATION_SCHEMA.COPY_HISTORY, a table function that requires a
    TABLE_NAME argument -- Snowflake exposes no way to ask for "this pipe's"
    history directly, so the caller resolves the pipe's target table first
    (`Repository.pipe_target_table`) and this just filters that table's
    history down to one pipe.

    TABLE_NAME and START_TIME are table-function named arguments, not an
    ordinary SELECT's WHERE clause. They are quote_literal'd/computed as
    plain SQL text, from before it was known that a pyformat bind works
    there too (verified live, see docs/snowflake-behavior.md "Copy
    history"). PIPE_NAME, filtering the function's *output*, is an ordinary
    WHERE value and binds normally.

    Each part of TABLE_NAME is quoted inside the literal, as
    `pipe_status` quotes its pipe names: unquoted, the string is
    upper-cased, so a lower-case or spaced table name was not found
    (verified live, see docs/snowflake-behavior.md "Copy history").

    `max_rows` bounds the result the same way `pipe_activity_history` does,
    and matters slightly more here: the PIPE_NAME filter is applied to the
    table function's output, so several pipes loading into one target table
    all have their history returned before anything is discarded.
    """
    target = f"{quote_identifier(target_catalog)}.INFORMATION_SCHEMA.COPY_HISTORY"
    qualified_table = quote_literal(
        ".".join(
            quote_identifier(part)
            for part in (target_catalog, target_schema, target_table)
        )
    )
    return (
        "SELECT FILE_NAME, FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, "
        "PIPE_RECEIVED_TIME, PIPE_NAME, FIRST_ERROR_MESSAGE, FIRST_ERROR_LINE_NUMBER "
        f"FROM TABLE({target}("
        f"TABLE_NAME => {qualified_table}, "
        "START_TIME => DATEADD('day', "
        f"-{PIPE_HISTORY_RETENTION_DAYS}, CURRENT_TIMESTAMP())"
        ")) "
        "WHERE PIPE_NAME = %s "
        f"ORDER BY LAST_LOAD_TIME DESC LIMIT {int(max_rows)}"
    )


def pipe_status(database: str, schema: str, names: Sequence[str]) -> str:
    """SYSTEM$PIPE_STATUS for each named pipe, one row per pipe.

    The function takes one pipe, and only as a constant: a column reference,
    as in a SELECT over INFORMATION_SCHEMA.PIPES, fails to compile (see
    docs/snowflake-behavior.md "Pipe execution state"). So the names are
    written into the statement, one SELECT per pipe joined with UNION ALL,
    which saves the round trips but not Snowflake's time per pipe. The
    argument is a string holding the quoted, fully qualified name, so a
    lower-case or spaced name resolves as written. Needs no warehouse.
    """
    return " UNION ALL ".join(
        f"SELECT {quote_literal(database)} AS PIPE_CATALOG_NAME, "
        f"{quote_literal(schema)} AS PIPE_SCHEMA_NAME, "
        f"{quote_literal(name)} AS PIPE_NAME, "
        "SYSTEM$PIPE_STATUS("
        + quote_literal(
            f"{quote_identifier(database)}.{quote_identifier(schema)}"
            f".{quote_identifier(name)}"
        )
        + ") AS PIPE_STATUS"
        for name in names
    )


def task_activity(window_seconds: int) -> str:
    """Every task's *latest* run in a schema, completed `window_seconds` ago or later.

    SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY, filtered to one schema's tasks and
    reduced to one row per task with QUALIFY -- the same shape `pipe_activity`
    uses, and for the same reason: a schema full of frequently-running tasks
    could otherwise return unboundedly many historical rows. A single task's
    full history (for its own detail view) is `task_activity_history` below
    instead -- lazy, per task, not part of this query.

    The window is `pipe_activity`'s, with the task history fetch window.
    It filters by COMPLETED_TIME, not SCHEDULED_TIME: ACCOUNT_USAGE lists a
    run only once it has finished, so a run that took five hours appears
    carrying a SCHEDULED_TIME five hours old, which a window pass would
    already have passed for good. "Latest" is still by SCHEDULED_TIME.
    ATTEMPT_NUMBER is selected for the key: QUERY_ID is NULL for a run that
    failed before a query was submitted (docs/snowflake-behavior.md "Task
    history").

    Like `pipe_activity`, this needs a running warehouse
    (see docs/snowflake-behavior.md) and IMPORTED PRIVILEGES on the SNOWFLAKE
    database (or ACCOUNTADMIN); either gap just fails this one scope, same as
    a missing warehouse does for `table_stats`.
    """
    return (
        "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, QUERY_ID, SCHEDULED_TIME, "
        "ATTEMPT_NUMBER, QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, "
        "ERROR_MESSAGE "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
        "WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s "
        f"AND COMPLETED_TIME >= {_since(window_seconds)} "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY NAME ORDER BY SCHEDULED_TIME DESC) = 1"
    )


def task_activity_history(window_seconds: int, max_rows: int) -> str:
    """One task's newest `max_rows` ACCOUNT_USAGE runs, completed `window_seconds` ago or later.

    The backfill half of a single task's detail view -- `task_activity` above
    deliberately only keeps the latest row per task, so a task's own History
    tab needs this instead, scoped to just that one task and fetched
    lazily only once that task's view is opened (`TaskView`), never eagerly
    for a whole schema. Like `pipe_activity_history`, this needs no
    target-table resolution: ACCOUNT_USAGE.TASK_HISTORY already carries NAME
    as an ordinary column, so this is just one more bound `%s` alongside
    `task_activity`'s two. `max_rows` bounds it for the same reason
    `pipe_activity_history` needs one: a minute-schedule task over a 90-day
    window is six figures of runs, and only the newest are ever displayed.
    Windowed by COMPLETED_TIME, for the reason given on `task_activity`.
    """
    return (
        "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, QUERY_ID, SCHEDULED_TIME, "
        "ATTEMPT_NUMBER, QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, "
        "ERROR_MESSAGE "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
        "WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s AND NAME = %s "
        f"AND COMPLETED_TIME >= {_since(window_seconds)} "
        f"ORDER BY SCHEDULED_TIME DESC LIMIT {int(max_rows)}"
    )


#: Snowflake's own hard retention limit for INFORMATION_SCHEMA.TASK_HISTORY --
#: a platform constant, not a preference, so it is not a Setting. Shorter than
#: PIPE_HISTORY_RETENTION_DAYS' 14 days.
TASK_HISTORY_RETENTION_DAYS = 7

#: The largest RESULT_LIMIT INFORMATION_SCHEMA.TASK_HISTORY accepts. Also a
#: platform constant, so the history row cap is clamped to it rather than
#: being allowed to build a statement Snowflake would reject.
TASK_HISTORY_RESULT_LIMIT_MAX = 10_000


def task_history(database: str, schema: str, task: str, max_rows: int) -> str:
    """One task's newest `max_rows` near-real-time runs, the last 7 days.

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
    connection pool" in docs/DESIGN.md), so it fails with "invalid identifier
    INFORMATION_SCHEMA.TASK_HISTORY" regardless of which database the task
    actually lives in. The DATABASE_NAME/SCHEMA_NAME named arguments below are
    a separate thing -- they filter the function's *output*, not its own
    name resolution.

    Every named argument here is a table-function argument, not an ordinary
    SELECT's WHERE clause, and all of them are quote_literal'd/computed as
    plain SQL text, as in `pipe_history` and for the same historical reason:
    a pyformat bind works there too, but that was verified only later. There
    is therefore no bound `%s` parameter at all here, unlike `pipe_history`,
    where PIPE_NAME filters the function's *output* rather than being a named
    argument itself.

    The outer ORDER BY ... LIMIT is what bounds the result, deterministically
    and to the newest runs: the function documents no ordering, so *which*
    rows RESULT_LIMIT alone keeps is undefined. RESULT_LIMIT is still set
    alongside it, clamped to the function's maximum, so the bound is pushed
    into the function too rather than only applied to its output.
    """
    target = f"{quote_identifier(database)}.INFORMATION_SCHEMA.TASK_HISTORY"
    result_limit = max(1, min(int(max_rows), TASK_HISTORY_RESULT_LIMIT_MAX))
    return (
        "SELECT NAME, QUERY_ID, SCHEDULED_TIME, ATTEMPT_NUMBER, QUERY_START_TIME, "
        "COMPLETED_TIME, STATE, ERROR_CODE, ERROR_MESSAGE "
        f"FROM TABLE({target}("
        f"DATABASE_NAME => {quote_literal(database)}, "
        f"SCHEMA_NAME => {quote_literal(schema)}, "
        f"TASK_NAME => {quote_literal(task)}, "
        f"RESULT_LIMIT => {result_limit}, "
        "SCHEDULED_TIME_RANGE_START => DATEADD('day', "
        f"-{TASK_HISTORY_RETENTION_DAYS}, CURRENT_TIMESTAMP())"
        ")) "
        f"ORDER BY SCHEDULED_TIME DESC LIMIT {int(max_rows)}"
    )


#: SHOW COLUMNS' type categories that INFORMATION_SCHEMA.COLUMNS.DATA_TYPE
#: names differently. Verified against a live account: these two are the only
#: ones, and `FIXED` is not even valid DDL -- it exists only inside this JSON.
#: Every other category (TEXT, BINARY, the timestamps, VARIANT, VECTOR, ...)
#: is already INFORMATION_SCHEMA's value and passes through unchanged.
_INFORMATION_SCHEMA_TYPES = {"FIXED": "NUMBER", "REAL": "FLOAT"}

#: Categories whose "scale" is a fractional-seconds count, not a decimal scale.
_DATETIME_TYPES = frozenset({"TIMESTAMP_LTZ", "TIMESTAMP_NTZ", "TIMESTAMP_TZ", "TIME"})

#: How DESC TABLE names a category, where that differs from the category.
_DESC_NAMES = {**_INFORMATION_SCHEMA_TYPES, "TEXT": "VARCHAR"}

#: A VECTOR's element type, which DESC TABLE writes the way it was declared:
#: VECTOR(INT, 768), not VECTOR(NUMBER(38,0), 768).
_VECTOR_ELEMENTS = {"FIXED": "INT", "REAL": "FLOAT"}


def _data_type_fields(raw: object) -> dict[str, object]:
    """Unpack SHOW COLUMNS' data_type, which is a JSON string.

    e.g. {"type":"TEXT","length":16777216,"nullable":true} or
    {"type":"FIXED","precision":38,"scale":0,"nullable":true}. Malformed or
    unexpected content degrades to "type unknown" rather than failing the
    whole fetch: a column nivaria cannot describe is still a column worth
    listing.
    """
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _type_arguments(fields: dict[str, Any], *keys: str) -> str:
    """ "(a,b)" from integer `keys` of `fields`, or "" unless all are present.

    All or nothing: NUMBER always reports precision and scale together, and
    half of them would render a malformed NUMBER(38) rather than the real type.
    """
    values = [fields.get(key) for key in keys]
    if not all(isinstance(value, int) for value in values):
        return ""
    return "(" + ",".join(str(value) for value in values) + ")"


def desc_type(fields: dict[str, Any]) -> str:
    """The type exactly as DESC TABLE prints it, from SHOW COLUMNS' JSON.

    INFORMATION_SCHEMA.COLUMNS cannot express a full type -- it has nowhere
    for a BINARY's length, a VECTOR's element and dimension, or a structured
    type's fields -- but SHOW's JSON carries all of it, and DESC TABLE's
    rendering is Snowflake's own. The rules were taken from live DESC TABLE
    output (tests/data/show_column_types.json), including the quirks: a
    VECTOR's element keeps its declared spelling, and NOT NULL appears only on
    an object's fields, because Snowflake rejects it on an array element or a
    map value and never prints it for a map key.

    Anything malformed degrades to the bare category, and no category at all
    to "UNKNOWN", rather than failing the fetch.
    """
    category = fields.get("type")
    if not isinstance(category, str):
        return "UNKNOWN"
    name = _DESC_NAMES.get(category, category)
    if category == "FIXED":
        return name + _type_arguments(fields, "precision", "scale")
    if category in ("TEXT", "BINARY"):
        return name + _type_arguments(fields, "length")
    if category in _DATETIME_TYPES:
        return name + _type_arguments(fields, "scale")
    if category == "DECFLOAT":
        return name + _type_arguments(fields, "precision")
    if category == "VECTOR":
        element = fields.get("vectorElementType")
        dimension = fields.get("dimension")
        if isinstance(element, dict) and isinstance(dimension, int):
            spelled = _VECTOR_ELEMENTS.get(element.get("type"))
            if spelled is not None:
                return f"{name}({spelled}, {dimension})"
        return name
    if category == "ARRAY":
        element = fields.get("elementType")
        return f"{name}({desc_type(element)})" if isinstance(element, dict) else name
    if category == "MAP":
        key, value = fields.get("keyType"), fields.get("valueType")
        if isinstance(key, dict) and isinstance(value, dict):
            return f"{name}({desc_type(key)}, {desc_type(value)})"
        return name
    if category == "OBJECT":
        members = fields.get("fields")
        if not isinstance(members, list):
            return name  # semi-structured OBJECT, as opposed to OBJECT()
        rendered = []
        for member in members:
            field_type = member.get("fieldType") if isinstance(member, dict) else None
            if not isinstance(field_type, dict):
                return name
            not_null = " NOT NULL" if field_type.get("nullable") is False else ""
            rendered.append(
                f"{member.get('fieldName', '')} {desc_type(field_type)}{not_null}"
            )
        return f"{name}({', '.join(rendered)})"
    return name


def map_show_columns(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map SHOW COLUMNS output onto the INFORMATION_SCHEMA.COLUMNS shape.

    The mirror keeps INFORMATION_SCHEMA's vocabulary because that is the
    documented, stable name for each of these fields; only the source changed.
    That goes for DATA_TYPE's values as well as its name: SHOW's internal
    `FIXED`/`REAL` are stored as the `NUMBER`/`FLOAT` INFORMATION_SCHEMA
    reports, so the cache never holds a type name no Snowflake DDL accepts.

    SHOW COLUMNS has no ORDINAL_POSITION -- it returns columns in declaration
    order instead -- so position is derived from that order, counted per table
    so a multi-table result does not keep incrementing across tables.

    `_desc_type` is the one field that is not INFORMATION_SCHEMA's: the full
    type as DESC TABLE prints it (see `desc_type`), underscored as nivaria's
    own column because INFORMATION_SCHEMA has no field that could hold it.
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
        category = _INFORMATION_SCHEMA_TYPES.get(category, category)
        # The JSON blob reports the same "precision"/"scale" keys for NUMBER's
        # decimal scale and for a timestamp/time's fractional-seconds count,
        # and a "length" for BINARY too; INFORMATION_SCHEMA keeps those apart
        # (and leaves every length field NULL for BINARY, verified live), so
        # the mirror gates each source key to the category it describes.
        is_datetime = category in _DATETIME_TYPES

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
                "CHARACTER_MAXIMUM_LENGTH": fields.get("length")
                if category == "TEXT"
                else None,
                "NUMERIC_PRECISION": fields.get("precision")
                if category == "NUMBER"
                else None,
                "NUMERIC_SCALE": fields.get("scale") if category == "NUMBER" else None,
                "DATETIME_PRECISION": fields.get("scale") if is_datetime else None,
                "COMMENT": row.get("comment"),
                "_desc_type": desc_type(fields),
            }
        )
    return mapped
