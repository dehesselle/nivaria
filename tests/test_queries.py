from __future__ import annotations

import pytest

from snowcone.gateway import queries
from snowcone.gateway.queries import quote_identifier, quote_literal
from snowcone.models import ObjectKind


def test_identifiers_are_double_quoted_to_preserve_case() -> None:
    """An unquoted lower-case identifier is folded to upper case by Snowflake,
    which would silently address a different object than the cache names."""
    assert quote_identifier("my_db") == '"my_db"'


def test_identifier_quoting_escapes_embedded_quotes() -> None:
    assert quote_identifier('we"ird') == '"we""ird"'


def test_literal_quoting_escapes_embedded_quotes() -> None:
    """The paging cursor is an object name interpolated as a literal, so a
    name containing an apostrophe must not be able to end the string."""
    assert quote_literal("O'BRIEN") == "'O''BRIEN'"


def test_show_databases_first_page_has_no_from_clause() -> None:
    assert queries.show_databases(limit=500) == "SHOW DATABASES LIMIT 500"


def test_show_databases_continues_after_the_cursor() -> None:
    assert queries.show_databases(after="ACME", limit=500) == (
        "SHOW DATABASES LIMIT 500 FROM 'ACME'"
    )


def test_show_schemas_scopes_to_a_quoted_database() -> None:
    assert queries.show_schemas("MYDB", limit=10) == (
        'SHOW SCHEMAS IN DATABASE "MYDB" LIMIT 10'
    )


def test_show_objects_uses_snowflakes_own_plural_noun() -> None:
    assert queries.show_objects(ObjectKind.PIPE, "MYDB", "PUBLIC", limit=10) == (
        'SHOW PIPES IN SCHEMA "MYDB"."PUBLIC" LIMIT 10'
    )


def test_show_objects_pages_with_a_keyset_cursor() -> None:
    assert (
        queries.show_objects(
            ObjectKind.TABLE, "MYDB", "PUBLIC", after="ORDERS", limit=1000
        )
        == 'SHOW TABLES IN SCHEMA "MYDB"."PUBLIC" LIMIT 1000 FROM \'ORDERS\''
    )


@pytest.mark.parametrize(("limit", "expected"), [(0, 1), (-5, 1), (50_000, 10_000)])
def test_page_limits_are_clamped_to_what_snowflake_accepts(limit, expected) -> None:
    """SHOW caps its result at 10k rows regardless of LIMIT; asking for more
    would quietly truncate a page and make the keyset cursor skip rows."""
    assert f"LIMIT {expected}" in queries.show_databases(limit=limit)


def test_show_warehouses_and_roles_are_unpaged() -> None:
    """Unlike the object lists, these are small flat lists with no LIMIT/FROM
    clause -- see the docstrings in gateway/queries.py."""
    assert queries.show_warehouses() == "SHOW WAREHOUSES"
    assert queries.show_roles() == "SHOW ROLES"


def test_use_role_and_warehouse_quote_the_identifier() -> None:
    assert queries.use_role("SYSADMIN") == 'USE ROLE "SYSADMIN"'
    assert queries.use_warehouse("my_wh") == 'USE WAREHOUSE "my_wh"'


def test_table_stats_targets_information_schema_tables_with_bound_params() -> None:
    """Unlike every SHOW builder above, TABLE_SCHEMA/TABLE_TYPE are left as
    driver bind-params rather than interpolated -- an ordinary SELECT accepts
    pyformat params where SHOW's `FROM 'literal'` cannot."""
    assert queries.table_stats("MYDB") == (
        "SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, LAST_DDL, LAST_ALTERED "
        'FROM "MYDB".INFORMATION_SCHEMA.TABLES '
        "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = %s"
    )


def test_pipe_activity_targets_account_usage_copy_history_with_bound_params() -> None:
    """Unlike table_stats, ACCOUNT_USAGE.COPY_HISTORY is a fixed name rather
    than one built from the database, so both filter values bind as ordinary
    params -- no quote_identifier involved at all. QUALIFY collapses the
    result to one row per pipe: the list only ever wants each pipe's latest
    load, and a single pipe's full history is pipe_activity_history instead
    (see repository._PIPE_ACTIVITY / _PIPE_ACTIVITY_HISTORY)."""
    assert queries.pipe_activity(90) == (
        "SELECT PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME, FILE_NAME, "
        "FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, TABLE_CATALOG_NAME, "
        "TABLE_SCHEMA_NAME, TABLE_NAME, FIRST_ERROR_MESSAGE, "
        "FIRST_ERROR_LINE_NUMBER "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY "
        "WHERE PIPE_CATALOG_NAME = %s AND PIPE_SCHEMA_NAME = %s "
        "AND LAST_LOAD_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP()) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY PIPE_NAME ORDER BY LAST_LOAD_TIME DESC) = 1"
    )


def test_pipe_activity_history_targets_one_pipe_with_bound_params() -> None:
    """The per-pipe backfill for a single pipe's detail view: same source and
    window as pipe_activity, but filtered to one PIPE_NAME instead of
    QUALIFY-reduced, since it needs the full history, not just the latest."""
    assert queries.pipe_activity_history(90) == (
        "SELECT PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME, FILE_NAME, "
        "FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, TABLE_CATALOG_NAME, "
        "TABLE_SCHEMA_NAME, TABLE_NAME, FIRST_ERROR_MESSAGE, "
        "FIRST_ERROR_LINE_NUMBER "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY "
        "WHERE PIPE_CATALOG_NAME = %s AND PIPE_SCHEMA_NAME = %s AND PIPE_NAME = %s "
        "AND LAST_LOAD_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())"
    )


def test_pipe_history_targets_information_schema_copy_history() -> None:
    """TABLE_NAME and START_TIME are table-function named arguments, not
    ordinary WHERE values, so they are quote_literal'd/computed as plain SQL
    text; PIPE_NAME filters the function's output and binds normally."""
    assert queries.pipe_history("MYDB", "PUBLIC", "ORDERS") == (
        "SELECT FILE_NAME, FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, PIPE_NAME, "
        "FIRST_ERROR_MESSAGE, FIRST_ERROR_LINE_NUMBER "
        'FROM TABLE("MYDB".INFORMATION_SCHEMA.COPY_HISTORY('
        "TABLE_NAME => 'MYDB.PUBLIC.ORDERS', "
        "START_TIME => DATEADD('day', -14, CURRENT_TIMESTAMP())"
        ")) "
        "WHERE PIPE_NAME = %s"
    )


def test_task_activity_targets_account_usage_task_history_with_bound_params() -> None:
    """Same shape as pipe_activity, against ACCOUNT_USAGE.TASK_HISTORY
    instead: DATABASE_NAME/SCHEMA_NAME bind as ordinary params, QUALIFY
    collapses the result to each task's latest run."""
    assert queries.task_activity(90) == (
        "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, QUERY_ID, SCHEDULED_TIME, "
        "QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, ERROR_MESSAGE "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
        "WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s "
        "AND SCHEDULED_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP()) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY NAME ORDER BY SCHEDULED_TIME DESC) = 1"
    )


def test_task_activity_history_targets_one_task_with_bound_params() -> None:
    """The per-task backfill: same source and window as task_activity, but
    filtered to one NAME instead of QUALIFY-reduced."""
    assert queries.task_activity_history(90) == (
        "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, QUERY_ID, SCHEDULED_TIME, "
        "QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, ERROR_MESSAGE "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
        "WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s AND NAME = %s "
        "AND SCHEDULED_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())"
    )


def test_task_history_targets_information_schema_task_history_with_named_args() -> None:
    """Unlike pipe_history, DATABASE_NAME/SCHEMA_NAME/TASK_NAME are all
    accepted as direct named arguments -- so this statement has no bound `%s`
    parameter at all, and no WHERE tail. The function name itself is still
    database-qualified, same as pipe_history qualifies COPY_HISTORY: a bare
    INFORMATION_SCHEMA.TASK_HISTORY resolves against whatever database
    happens to be current in the session, which the pool never sets -- see
    queries.task_history's docstring for the real-account error this caused
    before being qualified."""
    statement = queries.task_history("MYDB", "PUBLIC", "REFRESH_ORDERS")
    assert statement == (
        "SELECT NAME, QUERY_ID, SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME, "
        "STATE, ERROR_CODE, ERROR_MESSAGE "
        'FROM TABLE("MYDB".INFORMATION_SCHEMA.TASK_HISTORY('
        "DATABASE_NAME => 'MYDB', "
        "SCHEMA_NAME => 'PUBLIC', "
        "TASK_NAME => 'REFRESH_ORDERS', "
        "RESULT_LIMIT => 10000, "
        "SCHEDULED_TIME_RANGE_START => DATEADD('day', -7, CURRENT_TIMESTAMP())"
        "))"
    )
    assert "%s" not in statement
    assert "WHERE" not in statement
