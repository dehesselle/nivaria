from __future__ import annotations

import time

import pytest

from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    CacheState,
    ObjectKind,
    columns_scope,
    databases_scope,
    objects_scope,
    schemas_scope,
)


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


def db_rows(*names: str) -> list[dict]:
    return [{"name": name, "comment": f"c-{name}"} for name in names]


def table_rows(*names: str) -> list[dict]:
    return [
        {
            "database_name": "MYDB",
            "schema_name": "PUBLIC",
            "name": name,
            "kind": "TABLE",
            "rows": 10,
            "bytes": 2048,
            "comment": "",
        }
        for name in names
    ]


def load(repo: Repository, scope, rows) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


# ------------------------------------------------------------------ basics


def test_an_unknown_scope_reads_as_empty_rather_than_raising(repo) -> None:
    """The cache is an optimisation, never a dependency -- a scope nobody has
    fetched is 'no data', not an error on the UI path."""
    cached = repo.databases(databases_scope())
    assert cached.rows == []
    assert cached.state is CacheState.EMPTY
    assert cached.age() is None


def test_a_completed_pass_is_readable_and_fresh(repo) -> None:
    scope = databases_scope()
    load(repo, scope, db_rows("BETA", "ACME"))
    cached = repo.databases(scope)
    assert [d.name for d in cached.rows] == ["ACME", "BETA"]
    assert cached.state is CacheState.COMPLETE
    assert cached.age() < 5


def test_rows_come_back_in_name_order_regardless_of_arrival_order(repo) -> None:
    """The tree renders rows in the order the repository yields them, so the
    ordering has to live here rather than in every caller."""
    scope = databases_scope()
    load(repo, scope, db_rows("ZULU", "ALPHA", "MIKE"))
    assert [d.name for d in repo.databases(scope).rows] == ["ALPHA", "MIKE", "ZULU"]


def test_scopes_do_not_leak_into_each_other(repo) -> None:
    tables = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    other = objects_scope(ObjectKind.TABLE, "MYDB", "STAGING")
    load(repo, tables, table_rows("ORDERS"))
    load(
        repo,
        other,
        [{**row, "schema_name": "STAGING"} for row in table_rows("CUSTOMERS")],
    )
    assert [o.name for o in repo.objects(tables).rows] == ["ORDERS"]
    assert [o.name for o in repo.objects(other).rows] == ["CUSTOMERS"]


def test_tables_and_views_in_one_schema_are_separate_scopes(repo) -> None:
    """They share a schema but refresh independently; one must not sweep the
    other's rows away."""
    tables = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    views = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    load(repo, tables, table_rows("ORDERS"))
    load(
        repo,
        views,
        [
            {
                "database_name": "MYDB",
                "schema_name": "PUBLIC",
                "name": "ORDERS_V",
                "comment": "",
            }
        ],
    )
    assert [o.name for o in repo.objects(tables).rows] == ["ORDERS"]
    assert [o.name for o in repo.objects(views).rows] == ["ORDERS_V"]


# ------------------------------------------------------- the pass protocol


def test_pages_become_visible_before_the_pass_finishes(repo) -> None:
    """The whole point of chunking: a cold cache fills the tree in steps the
    user can see, rather than staying blank until the last page lands."""
    scope = databases_scope()
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, db_rows("ACME"), next_cursor="ACME")
    assert [d.name for d in repo.databases(scope).rows] == ["ACME"]
    assert repo.databases(scope).state is CacheState.PARTIAL

    repo.write_page(scope, generation, db_rows("BETA"), next_cursor=None)
    repo.finish_pass(scope, generation)
    assert [d.name for d in repo.databases(scope).rows] == ["ACME", "BETA"]


def test_a_refresh_never_shows_an_empty_list_on_the_way_to_a_full_one(repo) -> None:
    """Mark and sweep exists for exactly this: the rows from the previous pass
    stay readable throughout the new one."""
    scope = databases_scope()
    load(repo, scope, db_rows("ACME", "BETA"))

    generation = repo.begin_pass(scope)
    assert [d.name for d in repo.databases(scope).rows] == ["ACME", "BETA"]
    repo.write_page(scope, generation, db_rows("ACME"), next_cursor="ACME")
    assert [d.name for d in repo.databases(scope).rows] == ["ACME", "BETA"]
    repo.finish_pass(scope, generation)
    assert [d.name for d in repo.databases(scope).rows] == ["ACME"]


def test_a_deleted_object_disappears_after_the_sweep(repo) -> None:
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    load(repo, scope, table_rows("ORDERS", "CUSTOMERS"))
    load(repo, scope, table_rows("ORDERS"))
    assert [o.name for o in repo.objects(scope).rows] == ["ORDERS"]


def test_an_unfinished_pass_sweeps_nothing(repo) -> None:
    """A pass that crashed or was cancelled halfway must not be able to delete
    the rows it simply never got around to rewriting."""
    scope = databases_scope()
    load(repo, scope, db_rows("ACME", "BETA"))
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, db_rows("ACME"), next_cursor="ACME")
    assert [d.name for d in repo.databases(scope).rows] == ["ACME", "BETA"]


def test_the_age_shown_during_a_refresh_is_still_the_age_of_the_visible_rows(
    repo,
) -> None:
    """begin_pass carries fetched_at over deliberately -- the status bar is
    describing what is on screen, and that is still the old data."""
    scope = databases_scope()
    load(repo, scope, db_rows("ACME"))
    before = repo.entry(scope).fetched_at
    repo.begin_pass(scope)
    assert repo.entry(scope).fetched_at == before
    assert repo.databases(scope).age() is not None


def test_updating_an_existing_row_replaces_its_columns(repo) -> None:
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    load(repo, scope, table_rows("ORDERS"))
    changed = table_rows("ORDERS")
    changed[0]["rows"] = 99
    load(repo, scope, changed)
    assert repo.objects(scope).rows[0].rows == 99


def test_a_superseded_pass_cannot_write(repo) -> None:
    """Thread workers cannot be interrupted, so a job that was already
    in-flight when the user navigated away is fenced on arrival instead."""
    scope = databases_scope()
    stale = repo.begin_pass(scope)
    current = repo.begin_pass(scope)
    assert stale != current

    assert repo.write_page(scope, stale, db_rows("GHOST"), None) is False
    assert repo.databases(scope).rows == []
    assert repo.finish_pass(scope, stale) is False
    assert repo.fail_pass(scope, stale, "boom") is False

    assert repo.write_page(scope, current, db_rows("ACME"), None) is True
    assert repo.finish_pass(scope, current) is True
    assert [d.name for d in repo.databases(scope).rows] == ["ACME"]


def test_a_failed_pass_keeps_the_stale_rows_visible(repo) -> None:
    """Showing yesterday's tables beats showing an empty schema because the
    warehouse happened to be unreachable."""
    scope = databases_scope()
    load(repo, scope, db_rows("ACME"))
    generation = repo.begin_pass(scope)
    repo.fail_pass(scope, generation, "network down")
    cached = repo.databases(scope)
    assert [d.name for d in cached.rows] == ["ACME"]
    assert cached.state is CacheState.ERROR
    assert cached.entry.error == "network down"


def test_row_count_reflects_what_actually_survived_the_sweep(repo) -> None:
    scope = databases_scope()
    load(repo, scope, db_rows("ACME", "BETA", "GAMMA"))
    load(repo, scope, db_rows("ACME"))
    assert repo.entry(scope).row_count == 1


# ----------------------------------------------------------------- columns


def test_columns_keep_information_schemas_own_shape(repo) -> None:
    scope = columns_scope("MYDB", "PUBLIC", "ORDERS")
    load(
        repo,
        scope,
        [
            {
                "TABLE_CATALOG": "MYDB",
                "TABLE_SCHEMA": "PUBLIC",
                "TABLE_NAME": "ORDERS",
                "COLUMN_NAME": "ID",
                "ORDINAL_POSITION": 1,
                "DATA_TYPE": "NUMBER",
                "IS_NULLABLE": "NO",
                "COMMENT": "pk",
            }
        ],
    )
    column = repo.columns(scope).rows[0]
    assert column.column_name == "ID"
    assert column.data_type == "NUMBER"
    assert column.is_nullable is False
    assert column.comment == "pk"


def test_columns_are_ordered_by_position_not_name(repo) -> None:
    """A table's columns read in declaration order; alphabetising them would
    make a wide table unreadable against the real DDL."""
    scope = columns_scope("MYDB", "PUBLIC", "ORDERS")
    load(
        repo,
        scope,
        [
            {
                "TABLE_CATALOG": "MYDB",
                "TABLE_SCHEMA": "PUBLIC",
                "TABLE_NAME": "ORDERS",
                "COLUMN_NAME": name,
                "ORDINAL_POSITION": position,
                "DATA_TYPE": "TEXT",
                "IS_NULLABLE": "YES",
            }
            for position, name in enumerate(["ZIP", "ADDRESS", "ID"], start=1)
        ],
    )
    assert [c.column_name for c in repo.columns(scope).rows] == ["ZIP", "ADDRESS", "ID"]


# -------------------------------------------------------------- robustness


def test_unmirrored_snowflake_columns_are_dropped_not_fatal(repo) -> None:
    """Snowflake adds result columns over time; the mirror covers a useful
    subset on purpose and must not break when it sees an unfamiliar one."""
    scope = databases_scope()
    load(repo, scope, [{"name": "ACME", "some_new_column_snowflake_added": "x"}])
    assert [d.name for d in repo.databases(scope).rows] == ["ACME"]


def test_a_missing_column_in_a_row_becomes_null(repo) -> None:
    scope = databases_scope()
    load(repo, scope, [{"name": "ACME"}])
    assert repo.databases(scope).rows[0].comment == ""


def test_names_containing_quotes_survive_a_round_trip(repo) -> None:
    scope = databases_scope()
    load(repo, scope, db_rows('WE"IRD'))
    assert [d.name for d in repo.databases(scope).rows] == ['WE"IRD']


# ------------------------------------------------------------ child counts


def test_child_count_answers_a_collapsed_label_without_loading_rows(repo) -> None:
    scope = schemas_scope("MYDB")
    load(
        repo,
        scope,
        [
            {"database_name": "MYDB", "name": name, "comment": ""}
            for name in ("PUBLIC", "STAGING", "RAW")
        ],
    )
    assert repo.child_count(scope) == 3


def test_child_count_of_an_unknown_scope_is_zero(repo) -> None:
    assert repo.child_count(schemas_scope("NOPE")) == 0


# ---------------------------------------------------------------- staleness


def test_a_never_fetched_scope_counts_as_stale(repo) -> None:
    scope = databases_scope()
    assert repo.stale_scopes(900, [scope]) == [scope]


def test_a_fresh_scope_is_not_stale(repo) -> None:
    scope = databases_scope()
    load(repo, scope, db_rows("ACME"))
    assert repo.stale_scopes(900, [scope]) == []


def test_a_scope_older_than_the_ttl_is_stale(repo) -> None:
    scope = databases_scope()
    load(repo, scope, db_rows("ACME"))
    time.sleep(0.02)
    assert repo.stale_scopes(0.01, [scope]) == [scope]


def test_a_ttl_of_zero_disables_background_refreshing(repo) -> None:
    """0 is the documented off switch for the background sweep, so it must
    report nothing stale rather than everything."""
    assert repo.stale_scopes(0, [databases_scope(), schemas_scope("MYDB")]) == []


def test_only_the_scopes_offered_are_considered(repo) -> None:
    """Refreshing something nobody is looking at is exactly what this design
    is trying to avoid."""
    watched = databases_scope()
    unwatched = schemas_scope("MYDB")
    assert repo.stale_scopes(900, [watched]) == [watched]
    assert unwatched not in repo.stale_scopes(900, [watched])
