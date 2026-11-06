# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import pytest

from nivaria.models import ObjectType
from nivaria.snowflake import queries
from nivaria.snowflake.queries import quote_identifier, quote_literal


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
    assert queries.show_objects(ObjectType.VIEW, "MYDB", "PUBLIC", limit=10) == (
        'SHOW VIEWS IN SCHEMA "MYDB"."PUBLIC" LIMIT 10'
    )


def test_an_unpageable_type_is_asked_for_without_a_limit() -> None:
    """SHOW PIPES and SHOW STAGES accept LIMIT/FROM and ignore both, on two
    verified accounts. Emitting the clause anyway would log a bound that
    was never applied, and invite a caller to page a result that never
    shrinks -- which is exactly how the continuation chain used to hang."""
    for object_type in (ObjectType.PIPE, ObjectType.STAGE):
        statement = queries.show_objects(
            object_type, "MYDB", "PUBLIC", after="LOAD_ORDERS", limit=10
        )
        assert "LIMIT" not in statement
        assert "FROM" not in statement
        assert statement.endswith('IN SCHEMA "MYDB"."PUBLIC"')


def test_an_unpageable_type_is_asked_for_without_a_limit_in_a_database() -> None:
    """The whole-database prime carries the same finding -- SHOW PIPES IN
    DATABASE ... LIMIT 300 returned all 614 pipes on a real account."""
    statement = queries.show_objects_in_database(ObjectType.PIPE, "MYDB", limit=300)
    assert statement == 'SHOW PIPES IN DATABASE "MYDB"'


def test_a_pageable_type_still_gets_its_limit() -> None:
    """The finding is per object type, not a property of SHOW ... IN
    SCHEMA/IN DATABASE: tables and views honoured LIMIT correctly in the
    same session that caught pipes ignoring it."""
    for object_type in (ObjectType.TABLE, ObjectType.VIEW, ObjectType.TASK):
        assert "LIMIT 10" in queries.show_objects(
            object_type, "MYDB", "PUBLIC", limit=10
        )
        assert "LIMIT 10" in queries.show_objects_in_database(
            object_type, "MYDB", limit=10
        )


def test_show_objects_pages_with_a_keyset_cursor() -> None:
    assert (
        queries.show_objects(
            ObjectType.TABLE, "MYDB", "PUBLIC", after="ORDERS", limit=1000
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
    clause -- see the docstrings in snowflake/queries.py."""
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


def test_view_stats_is_the_exact_same_statement_as_table_stats() -> None:
    """TABLE_TYPE is a bound %s, not baked into the text -- the two builders
    differ only in what the caller binds ("BASE TABLE" vs "VIEW")."""
    assert queries.view_stats("MYDB") == queries.table_stats("MYDB")


def test_column_tag_references_excludes_tags_inherited_from_the_schema() -> None:
    """Without this WHERE clause, TAG_REFERENCES_ALL_COLUMNS reports a
    schema- or database-level tag against every column underneath it too --
    indistinguishable from a tag actually set on that column."""
    assert queries.tag_references_all_columns("MYDB") == (
        "SELECT TAG_DATABASE, TAG_SCHEMA, TAG_NAME, TAG_VALUE, COLUMN_NAME "
        "FROM TABLE(\"MYDB\".INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS(%s, 'table')) "
        "WHERE APPLY_METHOD IS DISTINCT FROM 'INHERITED'"
    )


def test_show_materialized_views_scopes_to_a_quoted_schema() -> None:
    assert queries.show_materialized_views("MYDB", "PUBLIC", limit=10) == (
        'SHOW MATERIALIZED VIEWS IN SCHEMA "MYDB"."PUBLIC" LIMIT 10'
    )


def test_show_materialized_views_pages_with_a_keyset_cursor() -> None:
    assert (
        queries.show_materialized_views("MYDB", "PUBLIC", after="TOTALS_MV", limit=1000)
        == 'SHOW MATERIALIZED VIEWS IN SCHEMA "MYDB"."PUBLIC" LIMIT 1000 FROM \'TOTALS_MV\''
    )


def test_pipe_activity_targets_account_usage_copy_history_with_bound_params() -> None:
    """Unlike table_stats, ACCOUNT_USAGE.COPY_HISTORY is a fixed name rather
    than one built from the database, so both filter values bind as ordinary
    params -- no quote_identifier involved at all. QUALIFY collapses the
    result to one row per pipe: the list only ever wants each pipe's latest
    load, and a single pipe's full history is pipe_activity_history instead
    (see repository._PIPE_ACTIVITY / _PIPE_ACTIVITY_HISTORY)."""
    assert queries.pipe_activity(14_400) == (
        "SELECT PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME, FILE_NAME, "
        "FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, TABLE_CATALOG_NAME, "
        "TABLE_SCHEMA_NAME, TABLE_NAME, FIRST_ERROR_MESSAGE, "
        "FIRST_ERROR_LINE_NUMBER "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY "
        "WHERE PIPE_CATALOG_NAME = %s AND PIPE_SCHEMA_NAME = %s "
        "AND LAST_LOAD_TIME >= DATEADD('second', -14400, CURRENT_TIMESTAMP()) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY PIPE_NAME ORDER BY LAST_LOAD_TIME DESC) = 1"
    )


def test_pipe_activity_history_targets_one_pipe_with_bound_params() -> None:
    """The per-pipe backfill for a single pipe's detail view: same source and
    window as pipe_activity, but filtered to one PIPE_NAME instead of
    QUALIFY-reduced, since it needs the full history, not just the latest.
    The window counts seconds back from Snowflake's own clock, so a window
    pass of a few hours and a full 90-day pass are the same statement."""
    assert queries.pipe_activity_history(14_400, 5000) == (
        "SELECT PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME, FILE_NAME, "
        "FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, PIPE_RECEIVED_TIME, "
        "TABLE_CATALOG_NAME, TABLE_SCHEMA_NAME, TABLE_NAME, FIRST_ERROR_MESSAGE, "
        "FIRST_ERROR_LINE_NUMBER "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY "
        "WHERE PIPE_CATALOG_NAME = %s AND PIPE_SCHEMA_NAME = %s AND PIPE_NAME = %s "
        "AND LAST_LOAD_TIME >= DATEADD('second', -14400, CURRENT_TIMESTAMP()) "
        "ORDER BY LAST_LOAD_TIME DESC LIMIT 5000"
    )


def test_pipe_history_targets_information_schema_copy_history() -> None:
    """TABLE_NAME and START_TIME are table-function named arguments, not
    ordinary WHERE values, so they are quote_literal'd/computed as plain SQL
    text; PIPE_NAME filters the function's output and binds normally."""
    assert queries.pipe_history("MYDB", "PUBLIC", "ORDERS", 5000) == (
        "SELECT FILE_NAME, FILE_SIZE, ROW_COUNT, STATUS, LAST_LOAD_TIME, "
        "PIPE_RECEIVED_TIME, PIPE_NAME, FIRST_ERROR_MESSAGE, FIRST_ERROR_LINE_NUMBER "
        'FROM TABLE("MYDB".INFORMATION_SCHEMA.COPY_HISTORY('
        """TABLE_NAME => '"MYDB"."PUBLIC"."ORDERS"', """
        "START_TIME => DATEADD('day', -14, CURRENT_TIMESTAMP())"
        ")) "
        "WHERE PIPE_NAME = %s "
        "ORDER BY LAST_LOAD_TIME DESC LIMIT 5000"
    )


def test_pipe_status_names_each_pipe_as_a_quoted_constant() -> None:
    """SYSTEM$PIPE_STATUS only takes a constant, so the names are written into
    the statement; quoting each identifier inside the literal keeps a
    lower-case or spaced name as written, verified live on the sandbox."""
    assert queries.pipe_status("MYDB", "PUBLIC", ["P_A", "it's lower"]) == (
        "SELECT 'MYDB' AS PIPE_CATALOG_NAME, 'PUBLIC' AS PIPE_SCHEMA_NAME, "
        "'P_A' AS PIPE_NAME, "
        'SYSTEM$PIPE_STATUS(\'"MYDB"."PUBLIC"."P_A"\') AS PIPE_STATUS '
        "UNION ALL "
        "SELECT 'MYDB' AS PIPE_CATALOG_NAME, 'PUBLIC' AS PIPE_SCHEMA_NAME, "
        "'it''s lower' AS PIPE_NAME, "
        'SYSTEM$PIPE_STATUS(\'"MYDB"."PUBLIC"."it\'\'s lower"\') AS PIPE_STATUS'
    )


def test_task_activity_targets_account_usage_task_history_with_bound_params() -> None:
    """Same shape as pipe_activity, against ACCOUNT_USAGE.TASK_HISTORY
    instead: DATABASE_NAME/SCHEMA_NAME bind as ordinary params, QUALIFY
    collapses the result to each task's latest run. The window is on
    COMPLETED_TIME, because a run is published only once it has finished,
    while "latest" stays by SCHEDULED_TIME."""
    assert queries.task_activity(5400) == (
        "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, QUERY_ID, SCHEDULED_TIME, "
        "ATTEMPT_NUMBER, QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, "
        "ERROR_MESSAGE "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
        "WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s "
        "AND COMPLETED_TIME >= DATEADD('second', -5400, CURRENT_TIMESTAMP()) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY NAME ORDER BY SCHEDULED_TIME DESC) = 1"
    )


def test_task_activity_history_targets_one_task_with_bound_params() -> None:
    """The per-task backfill: same source and window as task_activity, but
    filtered to one NAME instead of QUALIFY-reduced."""
    assert queries.task_activity_history(5400, 5000) == (
        "SELECT NAME, DATABASE_NAME, SCHEMA_NAME, QUERY_ID, SCHEDULED_TIME, "
        "ATTEMPT_NUMBER, QUERY_START_TIME, COMPLETED_TIME, STATE, ERROR_CODE, "
        "ERROR_MESSAGE "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY "
        "WHERE DATABASE_NAME = %s AND SCHEMA_NAME = %s AND NAME = %s "
        "AND COMPLETED_TIME >= DATEADD('second', -5400, CURRENT_TIMESTAMP()) "
        "ORDER BY SCHEDULED_TIME DESC LIMIT 5000"
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
    statement = queries.task_history("MYDB", "PUBLIC", "REFRESH_ORDERS", 5000)
    assert statement == (
        "SELECT NAME, QUERY_ID, SCHEDULED_TIME, ATTEMPT_NUMBER, QUERY_START_TIME, "
        "COMPLETED_TIME, STATE, ERROR_CODE, ERROR_MESSAGE "
        'FROM TABLE("MYDB".INFORMATION_SCHEMA.TASK_HISTORY('
        "DATABASE_NAME => 'MYDB', "
        "SCHEMA_NAME => 'PUBLIC', "
        "TASK_NAME => 'REFRESH_ORDERS', "
        "RESULT_LIMIT => 5000, "
        "SCHEDULED_TIME_RANGE_START => DATEADD('day', -7, CURRENT_TIMESTAMP())"
        ")) "
        "ORDER BY SCHEDULED_TIME DESC LIMIT 5000"
    )
    assert "%s" not in statement
    assert "WHERE" not in statement


def test_every_history_statement_takes_its_newest_rows_and_stops() -> None:
    """The four history statements are the only ones whose row count is
    driven by how *busy* an object is rather than how many objects exist, so
    they are the ones that can return six figures of rows for a single pipe.
    ORDER BY ... DESC is half the bound: without it LIMIT would keep an
    arbitrary slice, which is worse than no cap because it looks complete."""
    for statement, column in (
        (queries.pipe_activity_history(90, 250), "LAST_LOAD_TIME"),
        (queries.pipe_history("MYDB", "PUBLIC", "ORDERS", 250), "LAST_LOAD_TIME"),
        (queries.task_activity_history(90, 250), "SCHEDULED_TIME"),
        (queries.task_history("MYDB", "PUBLIC", "T", 250), "SCHEDULED_TIME"),
    ):
        assert statement.endswith(f"ORDER BY {column} DESC LIMIT 250"), statement


def test_the_task_history_function_argument_is_clamped_to_its_own_maximum() -> None:
    """RESULT_LIMIT is a table-function argument with a documented ceiling of
    10000; the row cap is an ordinary setting someone may raise past that. The
    outer LIMIT still carries whatever was asked for."""
    statement = queries.task_history("MYDB", "PUBLIC", "T", 50_000)
    assert "RESULT_LIMIT => 10000," in statement
    assert statement.endswith("ORDER BY SCHEDULED_TIME DESC LIMIT 50000")


def test_show_grants_on_object_names_the_type_and_quotes_every_part() -> None:
    assert (
        queries.show_grants_on_object(ObjectType.TABLE, "MyDb", "PUBLIC", "Orders")
        == 'SHOW GRANTS ON TABLE "MyDb"."PUBLIC"."Orders"'
    )


def test_a_data_preview_quotes_every_part_and_takes_its_limit() -> None:
    assert (
        queries.data_preview("MyDb", "PUBLIC", "Orders", 500)
        == 'SELECT * FROM "MyDb"."PUBLIC"."Orders" LIMIT 500'
    )


def test_pipe_history_quotes_each_part_of_the_table_name() -> None:
    """Unquoted, TABLE_NAME is upper-cased, so a lower-case or spaced table
    was not found; quoted parts resolve as written, verified live."""
    assert 'TABLE_NAME => \'"MYDB"."RAW"."t ""x"" it\'\'s"\'' in queries.pipe_history(
        "MYDB", "RAW", 't "x" it\'s', 5000
    )
