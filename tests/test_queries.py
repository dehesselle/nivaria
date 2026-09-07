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
