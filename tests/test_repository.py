# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from nivaria.cache.db import Database
from nivaria.cache.repository import Entry, Prune, Repository, Window, plan_window
from nivaria.models import (
    CacheState,
    ObjectType,
    columns_scope,
    data_preview_scope,
    databases_scope,
    objects_scope,
    pipe_activity_history_scope,
    pipe_activity_scope,
    roles_scope,
    schemas_scope,
    task_activity_history_scope,
    task_activity_scope,
    warehouses_scope,
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


def test_warehouses_and_roles_read_back_by_name(repo) -> None:
    """Same mapping shape as databases -- name plus comment -- since both are
    flat, unpaged SHOW results."""
    load(repo, warehouses_scope(), db_rows("COMPUTE_WH", "ETL_WH"))
    load(repo, roles_scope(), db_rows("SYSADMIN", "ACCOUNTADMIN"))
    assert [w.name for w in repo.warehouses(warehouses_scope()).rows] == [
        "COMPUTE_WH",
        "ETL_WH",
    ]
    assert [r.name for r in repo.roles(roles_scope()).rows] == [
        "ACCOUNTADMIN",
        "SYSADMIN",
    ]


def test_scopes_do_not_leak_into_each_other(repo) -> None:
    tables = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    other = objects_scope(ObjectType.TABLE, "MYDB", "STAGING")
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
    tables = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    views = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
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
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
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
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    load(repo, scope, table_rows("ORDERS"))
    changed = table_rows("ORDERS")
    changed[0]["rows"] = 99
    load(repo, scope, changed)
    # Through raw_rows, which is how every list view reads a SHOW column --
    # SFObject carries only what the tree needs (name, type, comment).
    assert repo.raw_rows(scope)[0]["rows"] == 99


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


def test_columns_carry_the_desc_type_through_the_cache(repo) -> None:
    scope = columns_scope("MYDB", "PUBLIC", "ORDERS")
    load(
        repo,
        scope,
        [
            {
                "TABLE_CATALOG": "MYDB",
                "TABLE_SCHEMA": "PUBLIC",
                "TABLE_NAME": "ORDERS",
                "COLUMN_NAME": "TOTAL",
                "ORDINAL_POSITION": 1,
                "DATA_TYPE": "NUMBER",
                "IS_NULLABLE": "NO",
                "NUMERIC_PRECISION": 38,
                "NUMERIC_SCALE": 0,
                "_desc_type": "NUMBER(38,0)",
            }
        ],
    )
    column = repo.columns(scope).rows[0]
    assert column.data_type == "NUMBER"
    assert column.desc_type == "NUMBER(38,0)"


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


def test_every_log_scope_ages_on_the_ordinary_cache_age(repo) -> None:
    """No scope is refreshed less often because its full statement is
    expensive: a log scope's refresh is a cheap window pass instead
    (repository.plan_window)."""
    ordinary = databases_scope()
    logs = [
        pipe_activity_scope("MYDB", "PUBLIC"),
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
        task_activity_scope("MYDB", "PUBLIC"),
        task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS"),
    ]
    for scope in [ordinary, *logs]:
        load(repo, scope, [])
    time.sleep(0.02)
    assert set(repo.stale_scopes(0.01, [ordinary, *logs])) == {ordinary, *logs}


def test_next_due_is_the_scope_closest_to_going_stale(repo) -> None:
    load(repo, databases_scope(), db_rows("ACME"))
    time.sleep(0.02)
    load(repo, schemas_scope("MYDB"), [])
    due = repo.next_due(900, [schemas_scope("MYDB"), databases_scope()])
    assert due < repo.next_due(900, [schemas_scope("MYDB")])


def test_next_due_of_a_never_fetched_scope_is_now(repo) -> None:
    assert repo.next_due(900, [databases_scope()]) == 0


def fail_old_rows(repo, scope, fetched_ago: float, attempted_ago: float) -> None:
    """Rows fetched `fetched_ago` seconds back, then a refresh that failed
    `attempted_ago` seconds back."""
    now = time.time()
    generation = repo.begin_pass(scope, now - fetched_ago)
    repo.write_page(scope, generation, db_rows("ACME"), None)
    repo.finish_pass(scope, generation)
    repo.fail_pass(scope, repo.begin_pass(scope, now - attempted_ago), "no warehouse")


def test_a_failed_refresh_is_retried_one_ttl_after_it_was_attempted(repo) -> None:
    """Its rows are long overdue, but aged by them every sweep would rerun the
    failing statement. The sweep and the countdown agree on it."""
    scope = databases_scope()
    fail_old_rows(repo, scope, fetched_ago=5000, attempted_ago=100)
    assert repo.stale_scopes(900, [scope]) == []
    assert 799 < repo.next_due(900, [scope]) <= 800


def test_a_failed_refresh_is_due_once_its_attempt_is_a_ttl_old(repo) -> None:
    scope = databases_scope()
    fail_old_rows(repo, scope, fetched_ago=5000, attempted_ago=1000)
    assert repo.stale_scopes(900, [scope]) == [scope]
    assert repo.next_due(900, [scope]) <= 0


def test_rows_count_as_fetched_when_their_pass_began(repo) -> None:
    """A pass of many pages ends later than its first page was asked for;
    stamping the end would let those rows go stale before the countdown
    said so."""
    scope = databases_scope()
    started = time.time() - 30
    generation = repo.begin_pass(scope, started)
    repo.write_page(scope, generation, db_rows("ACME"), "ACME")
    repo.write_page(scope, generation, db_rows("BETA"), None)
    repo.finish_pass(scope, generation)
    assert repo.entry(scope).fetched_at == started


def test_a_failed_refresh_is_listed_as_failed_and_keeps_its_rows_age(repo) -> None:
    scope = databases_scope()
    fail_old_rows(repo, scope, fetched_ago=5000, attempted_ago=100)
    entry = repo.entry(scope)
    assert entry.age() > 4999
    assert repo.failed_scopes([scope, schemas_scope("MYDB")]) == [scope]


def test_next_due_is_none_when_refreshing_is_off(repo) -> None:
    assert repo.next_due(0, [databases_scope()]) is None


# ------------------------------------------- schema.sql / TableSpec agreement


def test_every_mirrored_column_exists_in_the_schema(repo) -> None:
    """schema.sql and the TableSpecs are two hand-written lists of the same
    columns. A column in a spec but not the DDL fails only when that object
    type is first fetched, which could be days after the typo."""
    from nivaria.cache.repository import (
        _COLUMNS,
        _DATABASES,
        _GRANTS,
        _MATERIALIZED_VIEW_STATS,
        _OBJECT_TABLES,
        _PIPE_ACTIVITY,
        _PIPE_ACTIVITY_HISTORY,
        _PIPE_HISTORY,
        _SCHEMAS,
        _TABLE_STATS,
        _TAG_REFERENCES,
        _TASK_ACTIVITY,
        _TASK_ACTIVITY_HISTORY,
        _TASK_HISTORY,
        _VIEW_STATS,
    )

    specs = [
        _DATABASES,
        _SCHEMAS,
        _COLUMNS,
        _TABLE_STATS,
        _VIEW_STATS,
        _MATERIALIZED_VIEW_STATS,
        _PIPE_ACTIVITY,
        _PIPE_ACTIVITY_HISTORY,
        _PIPE_HISTORY,
        _TASK_ACTIVITY,
        _TASK_ACTIVITY_HISTORY,
        _TASK_HISTORY,
        _GRANTS,
        _TAG_REFERENCES,
        *_OBJECT_TABLES.values(),
    ]
    with repo._db.connect() as connection:
        for spec in specs:
            actual = {
                row["name"].lower()
                for row in connection.execute(f"PRAGMA table_info({spec.name})")
            }
            declared = {column.lower() for column in spec.columns}
            missing = declared - actual
            assert not missing, f"{spec.name} is missing {sorted(missing)}"


def test_every_object_kind_has_a_mirror_table(repo) -> None:
    from nivaria.cache.repository import _OBJECT_TABLES
    from nivaria.models import ObjectType

    assert set(_OBJECT_TABLES) == set(ObjectType)


def test_a_verified_snowflake_column_survives_a_round_trip(repo) -> None:
    """The columns below come from a real SHOW TABLES result, so this pins the
    mirror against the shape the account actually returns."""
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "created_on": "2026-01-01 00:00:00",
                "name": "ORDERS",
                "database_name": "MYDB",
                "schema_name": "PUBLIC",
                "kind": "TABLE",
                "comment": "",
                "cluster_by": "",
                "rows": 42,
                "bytes": 8192,
                "owner": "ANALYST",
                "retention_time": "1",
                "automatic_clustering": "OFF",
                "change_tracking": "OFF",
                "search_optimization": "OFF",
                "is_external": "N",
                "is_event": "N",
                "is_hybrid": "N",
                "is_iceberg": "N",
                "is_dynamic": "N",
                "owner_role_type": "ROLE",
                # Columns nivaria does not mirror, present in the real result.
                "search_optimization_progress": None,
                "enable_schema_evolution": "N",
                "is_immutable": "N",
                "row_timestamp": None,
                "error_logging": None,
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["is_iceberg"] == "N"
    assert stored["rows"] == 42
    assert "is_immutable" not in stored


def test_table_stats_columns_survive_a_round_trip(repo) -> None:
    """INFORMATION_SCHEMA.TABLES' LAST_DDL/LAST_ALTERED, under the names a
    live account returned them on 2026-09-09 (docs/snowflake-behavior.md)."""
    from nivaria.models import table_stats_scope

    scope = table_stats_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "TABLE_CATALOG": "MYDB",
                "TABLE_SCHEMA": "PUBLIC",
                "TABLE_NAME": "ORDERS",
                "LAST_DDL": "2026-01-01 00:00:00",
                "LAST_ALTERED": "2026-01-02 00:00:00",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["LAST_DDL"] == "2026-01-01 00:00:00"
    assert stored["LAST_ALTERED"] == "2026-01-02 00:00:00"


def test_view_stats_columns_survive_a_round_trip(repo) -> None:
    """The same INFORMATION_SCHEMA.TABLES statement as
    test_table_stats_columns_survive_a_round_trip, filtered to TABLE_TYPE =
    'VIEW' instead of 'BASE TABLE', into its own mirror table."""
    from nivaria.models import view_stats_scope

    scope = view_stats_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "TABLE_CATALOG": "MYDB",
                "TABLE_SCHEMA": "PUBLIC",
                "TABLE_NAME": "ORDERS_V",
                "LAST_DDL": "2026-01-01 00:00:00",
                "LAST_ALTERED": "2026-01-02 00:00:00",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["LAST_DDL"] == "2026-01-01 00:00:00"
    assert stored["LAST_ALTERED"] == "2026-01-02 00:00:00"


def test_materialized_view_stats_columns_survive_a_round_trip(repo) -> None:
    """SHOW MATERIALIZED VIEWS' rows/bytes, under the names a live account
    returned them on 2026-09-09 (docs/snowflake-behavior.md)."""
    from nivaria.models import materialized_view_stats_scope

    scope = materialized_view_stats_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "database_name": "MYDB",
                "schema_name": "PUBLIC",
                "name": "TOTALS_MV",
                "rows": 42,
                "bytes": 8192,
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["rows"] == 42
    assert stored["bytes"] == 8192


def test_pipe_activity_columns_survive_a_round_trip(repo) -> None:
    """ACCOUNT_USAGE.COPY_HISTORY's columns, under the names a live account
    returned them on 2026-09-09 (docs/snowflake-behavior.md)."""
    from nivaria.models import pipe_activity_scope

    scope = pipe_activity_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "MY_PIPE",
                "FILE_NAME": "orders_1.csv.gz",
                "FILE_SIZE": 1024,
                "ROW_COUNT": 42,
                "STATUS": "Loaded",
                "LAST_LOAD_TIME": "2026-01-02 03:04:05",
                "TABLE_CATALOG_NAME": "MYDB",
                "TABLE_SCHEMA_NAME": "PUBLIC",
                "TABLE_NAME": "ORDERS",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["FILE_NAME"] == "orders_1.csv.gz"
    assert stored["STATUS"] == "Loaded"
    assert stored["TABLE_NAME"] == "ORDERS"


def test_grants_columns_survive_a_round_trip(repo) -> None:
    """SHOW GRANTS' columns, granted_by_role_type included, under the names a
    live account returned them on 2026-09-09 (docs/snowflake-behavior.md)."""
    from nivaria.models import grants_scope

    scope = grants_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "created_on": "2026-01-01 00:00:00",
                "privilege": "USAGE",
                "granted_on": "SCHEMA",
                "name": "MYDB.PUBLIC",
                "granted_to": "DATABASE ROLE",
                "grantee_name": "APP_ROLE",
                "grant_option": "false",
                "granted_by": "SYSADMIN",
                "granted_by_role_type": "ROLE",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["granted_to"] == "DATABASE ROLE"
    assert stored["privilege"] == "USAGE"


def test_a_schema_level_and_a_database_level_grants_scope_do_not_collide(repo) -> None:
    """The same physical table holds both -- _scope in the key must actually
    keep them apart."""
    from nivaria.models import grants_scope

    schema_level = grants_scope("MYDB", "PUBLIC")
    database_level = grants_scope("MYDB")
    load(
        repo,
        schema_level,
        [
            {
                "privilege": "USAGE",
                "granted_on": "SCHEMA",
                "name": "MYDB.PUBLIC",
                "granted_to": "ROLE",
                "grantee_name": "SCHEMA_ROLE",
            }
        ],
    )
    load(
        repo,
        database_level,
        [
            {
                "privilege": "USAGE",
                "granted_on": "DATABASE",
                "name": "MYDB",
                "granted_to": "ROLE",
                "grantee_name": "DATABASE_ROLE",
            }
        ],
    )
    assert [r["grantee_name"] for r in repo.raw_rows(schema_level)] == ["SCHEMA_ROLE"]
    assert [r["grantee_name"] for r in repo.raw_rows(database_level)] == [
        "DATABASE_ROLE"
    ]


def test_an_object_level_and_a_schema_level_grants_scope_do_not_collide(repo) -> None:
    from nivaria.models import ObjectRef, ObjectType, grants_scope, object_grants_scope

    schema_level = grants_scope("MYDB", "PUBLIC")
    object_level = object_grants_scope(
        ObjectRef(ObjectType.TABLE, "MYDB", "PUBLIC", "ORDERS")
    )
    load(
        repo,
        schema_level,
        [
            {
                "privilege": "USAGE",
                "granted_on": "SCHEMA",
                "name": "MYDB.PUBLIC",
                "granted_to": "ROLE",
                "grantee_name": "SCHEMA_ROLE",
            }
        ],
    )
    load(
        repo,
        object_level,
        [
            {
                "privilege": "SELECT",
                "granted_on": "TABLE",
                "name": "MYDB.PUBLIC.ORDERS",
                "granted_to": "ROLE",
                "grantee_name": "TABLE_ROLE",
            }
        ],
    )
    assert [r["grantee_name"] for r in repo.raw_rows(schema_level)] == ["SCHEMA_ROLE"]
    assert [r["grantee_name"] for r in repo.raw_rows(object_level)] == ["TABLE_ROLE"]


def test_tag_references_columns_survive_a_round_trip(repo) -> None:
    """INFORMATION_SCHEMA.TAG_REFERENCES' columns, under the names a live
    account returned them on 2026-09-09 (docs/snowflake-behavior.md)."""
    from nivaria.models import tag_references_scope

    scope = tag_references_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "TAG_DATABASE": "MYDB",
                "TAG_SCHEMA": "TAGS",
                "TAG_NAME": "COST_CENTER",
                "TAG_VALUE": "ENG",
                "DOMAIN": "SCHEMA",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["TAG_NAME"] == "COST_CENTER"
    assert stored["TAG_VALUE"] == "ENG"


def test_a_multi_value_tag_keeps_every_value(repo) -> None:
    from nivaria.models import tag_references_scope

    scope = tag_references_scope("MYDB", "PUBLIC")
    tag = {
        "TAG_DATABASE": "MYDB",
        "TAG_SCHEMA": "TAGS",
        "TAG_NAME": "COST_CENTER",
        "DOMAIN": "SCHEMA",
    }
    load(repo, scope, [{**tag, "TAG_VALUE": "ENG"}, {**tag, "TAG_VALUE": "OPS"}])
    assert [r["TAG_VALUE"] for r in repo.raw_rows(scope)] == ["ENG", "OPS"]


def test_a_multi_value_column_tag_keeps_every_value(repo) -> None:
    from nivaria.models import column_tag_references_scope

    scope = column_tag_references_scope("MYDB", "PUBLIC", "ORDERS")
    tag = {
        "TAG_DATABASE": "MYDB",
        "TAG_SCHEMA": "TAGS",
        "TAG_NAME": "PII",
        "COLUMN_NAME": "EMAIL",
    }
    load(repo, scope, [{**tag, "TAG_VALUE": "phone"}, {**tag, "TAG_VALUE": "email"}])
    assert [r["TAG_VALUE"] for r in repo.raw_rows(scope)] == ["email", "phone"]


def test_pipe_activity_table_can_hold_more_than_one_row_per_pipe(repo) -> None:
    """The mirror table's PK is (pipe, FILE_NAME, LAST_LOAD_TIME), not just
    (pipe) -- queries.pipe_activity's QUALIFY means only one row per pipe is
    ever fetched in practice now, but the wider key is still what lets a pass
    that advances a pipe's latest load insert the new row under its own key
    rather than collide with the previous one, which finish_pass then sweeps
    away as stale. (A single pipe's full history lives in
    account_usage_copy_history_detail instead -- see _PIPE_ACTIVITY_HISTORY.)
    """
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "MY_PIPE",
                "FILE_NAME": "orders_1.csv.gz",
                "FILE_SIZE": 1024,
                "ROW_COUNT": 42,
                "STATUS": "Loaded",
                "LAST_LOAD_TIME": "2026-01-02 03:04:05",
                "TABLE_CATALOG_NAME": "MYDB",
                "TABLE_SCHEMA_NAME": "PUBLIC",
                "TABLE_NAME": "ORDERS",
            },
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "MY_PIPE",
                "FILE_NAME": "orders_2.csv.gz",
                "FILE_SIZE": 2048,
                "ROW_COUNT": 84,
                "STATUS": "Loaded",
                "LAST_LOAD_TIME": "2026-01-03 03:04:05",
                "TABLE_CATALOG_NAME": "MYDB",
                "TABLE_SCHEMA_NAME": "PUBLIC",
                "TABLE_NAME": "ORDERS",
            },
        ],
    )
    stored = repo.raw_rows(scope)
    assert {row["FILE_NAME"] for row in stored} == {
        "orders_1.csv.gz",
        "orders_2.csv.gz",
    }


def test_pipe_activity_history_columns_round_trip(repo) -> None:
    """The per-pipe backfill mirror -- same column shape as account_usage_
    copy_history, verified live alongside it on 2026-09-09."""
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", "MY_PIPE")
    load(
        repo,
        scope,
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "MY_PIPE",
                "FILE_NAME": "orders_1.csv.gz",
                "FILE_SIZE": 1024,
                "ROW_COUNT": 42,
                "STATUS": "Loaded",
                "LAST_LOAD_TIME": "2026-01-02 03:04:05",
                "TABLE_CATALOG_NAME": "MYDB",
                "TABLE_SCHEMA_NAME": "PUBLIC",
                "TABLE_NAME": "ORDERS",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["FILE_NAME"] == "orders_1.csv.gz"
    assert stored["STATUS"] == "Loaded"


def test_pipe_activity_history_is_scoped_per_pipe_not_per_schema(repo) -> None:
    """Unlike account_usage_copy_history (one _scope per schema),
    account_usage_copy_history_detail is one _scope per pipe -- a row cached
    for one pipe must not be visible from another pipe's scope."""
    load(
        repo,
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_OTHER"),
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "LOAD_OTHER",
                "FILE_NAME": "other.csv.gz",
                "LAST_LOAD_TIME": "2026-01-02 03:04:05",
            }
        ],
    )
    assert (
        repo.raw_rows(pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"))
        == []
    )


def test_pipe_target_table_is_none_with_no_activity_cached(repo) -> None:
    """A pipe that has never loaded anything (or whose schema's activity
    scope has never been fetched) has nothing to resolve a target from --
    None, not an error, is the correct answer here."""
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS") is None


def test_pipe_target_table_ignores_another_pipes_activity_rows(repo) -> None:
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "LOAD_OTHER",
                "FILE_NAME": "other.csv.gz",
                "LAST_LOAD_TIME": "2026-01-02 03:04:05",
                "TABLE_CATALOG_NAME": "MYDB",
                "TABLE_SCHEMA_NAME": "PUBLIC",
                "TABLE_NAME": "OTHER",
            }
        ],
    )
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS") is None


def test_pipe_target_table_reads_snowflakes_own_resolution(repo) -> None:
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "LOAD_ORDERS",
                "FILE_NAME": "orders_1.csv.gz",
                "LAST_LOAD_TIME": "2026-01-02 03:04:05",
                "TABLE_CATALOG_NAME": "MYDB",
                "TABLE_SCHEMA_NAME": "RAW",
                "TABLE_NAME": "ORDERS",
            }
        ],
    )
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS") == (
        "MYDB",
        "RAW",
        "ORDERS",
    )


def _pipe_defined_as(repo, definition: str) -> None:
    load(
        repo,
        objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC"),
        [
            {
                "database_name": "MYDB",
                "schema_name": "PUBLIC",
                "name": "LOAD_ORDERS",
                "definition": definition,
            }
        ],
    )


def test_a_pipe_without_activity_takes_its_target_from_its_definition(repo) -> None:
    """A pipe that never loaded has no PIPE_ACTIVITY row, and without a
    target its 14-day history would never be asked for."""
    _pipe_defined_as(repo, 'COPY INTO mydb.raw."orders" FROM @s')
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS") == (
        "MYDB",
        "RAW",
        "orders",
    )


def test_a_partly_qualified_definition_gives_no_target(repo) -> None:
    """Resolved against the session's schema at CREATE PIPE time, which the
    text does not record."""
    _pipe_defined_as(repo, "COPY INTO raw.orders FROM @s")
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS") is None


def test_snowflakes_own_resolution_wins_over_the_definition(repo) -> None:
    _pipe_defined_as(repo, "COPY INTO mydb.raw.elsewhere FROM @s")
    load(
        repo,
        pipe_activity_scope("MYDB", "PUBLIC"),
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "LOAD_ORDERS",
                "FILE_NAME": "orders_1.csv.gz",
                "LAST_LOAD_TIME": "2026-01-02 03:04:05",
                "TABLE_CATALOG_NAME": "MYDB",
                "TABLE_SCHEMA_NAME": "RAW",
                "TABLE_NAME": "ORDERS",
            }
        ],
    )
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS") == (
        "MYDB",
        "RAW",
        "ORDERS",
    )


def _other_pipes_activity(repo) -> None:
    load(
        repo,
        pipe_activity_scope("MYDB", "PUBLIC"),
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "LOAD_OTHER",
                "FILE_NAME": "other.csv.gz",
                "LAST_LOAD_TIME": "2026-01-02 03:04:05",
            }
        ],
    )


def test_an_unfetched_snapshot_says_nothing_about_a_pipe_being_idle(repo) -> None:
    assert repo.pipe_idle_as_of("MYDB", "PUBLIC", "LOAD_ORDERS") is None


def test_a_fetched_snapshot_without_the_pipe_says_it_was_idle_then(repo) -> None:
    _other_pipes_activity(repo)
    fetched_at = repo.entry(pipe_activity_scope("MYDB", "PUBLIC")).fetched_at
    assert fetched_at is not None
    assert repo.pipe_idle_as_of("MYDB", "PUBLIC", "LOAD_ORDERS") == fetched_at


def test_a_snapshot_with_the_pipes_load_does_not_call_it_idle(repo) -> None:
    _other_pipes_activity(repo)
    assert repo.pipe_idle_as_of("MYDB", "PUBLIC", "LOAD_OTHER") is None


def test_a_refreshing_snapshot_still_says_the_pipe_was_idle(repo) -> None:
    """A refresh keeps the earlier rows until it finishes, so their answer
    still holds as of when they were fetched."""
    _other_pipes_activity(repo)
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    fetched_at = repo.entry(scope).fetched_at
    repo.begin_pass(scope)
    assert repo.pipe_idle_as_of("MYDB", "PUBLIC", "LOAD_ORDERS") == fetched_at


def test_pipe_history_columns_survive_a_round_trip(repo) -> None:
    """INFORMATION_SCHEMA.COPY_HISTORY's columns, under the names a live
    account returned them on 2026-09-09 (docs/snowflake-behavior.md)."""
    from nivaria.models import pipe_history_scope

    scope = pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS")
    load(
        repo,
        scope,
        [
            {
                "FILE_NAME": "orders_9.csv.gz",
                "FILE_SIZE": 2048,
                "ROW_COUNT": 100,
                "STATUS": "Loaded",
                "LAST_LOAD_TIME": "2026-03-01 08:00:00",
                "PIPE_NAME": "LOAD_ORDERS",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["FILE_NAME"] == "orders_9.csv.gz"
    assert stored["STATUS"] == "Loaded"


def test_task_activity_columns_survive_a_round_trip(repo) -> None:
    """ACCOUNT_USAGE.TASK_HISTORY's columns, under the names a live account
    returned them on 2026-09-09 (docs/snowflake-behavior.md)."""
    scope = task_activity_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "NAME": "REFRESH_ORDERS",
                "DATABASE_NAME": "MYDB",
                "SCHEMA_NAME": "PUBLIC",
                "QUERY_ID": "01ab-query-id",
                "SCHEDULED_TIME": "2026-01-02 03:04:05",
                "ATTEMPT_NUMBER": 1,
                "QUERY_START_TIME": "2026-01-02 03:04:06",
                "COMPLETED_TIME": "2026-01-02 03:04:10",
                "STATE": "SUCCEEDED",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["NAME"] == "REFRESH_ORDERS"
    assert stored["STATE"] == "SUCCEEDED"


def test_task_activity_table_can_hold_more_than_one_task_per_run(repo) -> None:
    """The mirror table's PK is (database, schema, task, QUERY_ID), not just
    (database, schema, task) -- queries.task_activity's QUALIFY means only
    one row per task is ever fetched in practice now, but the wider key is
    still what lets a pass that advances a task's latest run insert the new
    row under its own key rather than collide with the previous one, which
    finish_pass then sweeps away as stale. (A single task's full history
    lives in account_usage_task_history_detail instead -- see
    _TASK_ACTIVITY_HISTORY.)"""
    scope = task_activity_scope("MYDB", "PUBLIC")
    load(
        repo,
        scope,
        [
            {
                "NAME": "REFRESH_ORDERS",
                "DATABASE_NAME": "MYDB",
                "SCHEMA_NAME": "PUBLIC",
                "QUERY_ID": "01ab-query-id-1",
                "SCHEDULED_TIME": "2026-01-02 03:04:05",
                "ATTEMPT_NUMBER": 1,
                "STATE": "SUCCEEDED",
            },
            {
                "NAME": "REFRESH_TOTALS",
                "DATABASE_NAME": "MYDB",
                "SCHEMA_NAME": "PUBLIC",
                "QUERY_ID": "01ab-query-id-2",
                "SCHEDULED_TIME": "2026-01-02 03:05:05",
                "ATTEMPT_NUMBER": 1,
                "STATE": "FAILED",
            },
        ],
    )
    stored = repo.raw_rows(scope)
    assert {row["NAME"] for row in stored} == {"REFRESH_ORDERS", "REFRESH_TOTALS"}


def test_task_activity_history_columns_round_trip(repo) -> None:
    """The per-task backfill mirror -- same column shape as account_usage_
    task_history, verified live alongside it on 2026-09-09."""
    scope = task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS")
    load(
        repo,
        scope,
        [
            {
                "NAME": "REFRESH_ORDERS",
                "DATABASE_NAME": "MYDB",
                "SCHEMA_NAME": "PUBLIC",
                "QUERY_ID": "01ab-query-id",
                "SCHEDULED_TIME": "2026-01-02 03:04:05",
                "ATTEMPT_NUMBER": 1,
                "STATE": "SUCCEEDED",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["NAME"] == "REFRESH_ORDERS"
    assert stored["STATE"] == "SUCCEEDED"


def test_task_activity_history_is_scoped_per_task_not_per_schema(repo) -> None:
    """Unlike account_usage_task_history (one _scope per schema),
    account_usage_task_history_detail is one _scope per task -- a row cached
    for one task must not be visible from another task's scope."""
    load(
        repo,
        task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_OTHER"),
        [
            {
                "NAME": "REFRESH_OTHER",
                "QUERY_ID": "01ab-other",
                "SCHEDULED_TIME": "2026-01-02 03:04:05",
                "ATTEMPT_NUMBER": 1,
            }
        ],
    )
    assert (
        repo.raw_rows(task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS"))
        == []
    )


def test_task_history_columns_survive_a_round_trip(repo) -> None:
    """INFORMATION_SCHEMA.TASK_HISTORY's columns, under the names a live
    account returned them on 2026-09-09 (docs/snowflake-behavior.md)."""
    from nivaria.models import task_history_scope

    scope = task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS")
    load(
        repo,
        scope,
        [
            {
                "NAME": "REFRESH_ORDERS",
                "QUERY_ID": "01ab-query-id",
                "SCHEDULED_TIME": "2026-03-01 08:00:00",
                "ATTEMPT_NUMBER": 1,
                "QUERY_START_TIME": "2026-03-01 08:00:01",
                "COMPLETED_TIME": "2026-03-01 08:00:05",
                "STATE": "SUCCEEDED",
            }
        ],
    )
    stored = repo.raw_rows(scope)[0]
    assert stored["NAME"] == "REFRESH_ORDERS"
    assert stored["STATE"] == "SUCCEEDED"


def test_a_pass_is_not_truncated_unless_it_says_so(repo) -> None:
    """The default has to be the honest one: a scope nobody told anything
    about must not claim rows are missing."""
    scope = databases_scope()
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, [{"name": "ACME"}], None)
    repo.finish_pass(scope, generation)
    assert not repo.entry(scope).truncated


def test_a_truncated_pass_is_still_complete_not_an_error(repo) -> None:
    """Truncated is a third thing, and the distinction is the whole point:
    ERROR means nothing arrived and the sweep must not run; truncated means
    the newest rows arrived, were swept in normally, and there are simply more
    of them upstream."""
    scope = databases_scope()
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, [{"name": "ACME"}], None)
    repo.finish_pass(scope, generation, truncated=True)
    entry = repo.entry(scope)
    assert entry.truncated
    assert entry.state is CacheState.COMPLETE
    assert entry.error is None


def test_a_new_pass_clears_a_previous_truncation(repo) -> None:
    """Raising the cap, or a pipe going quiet, has to be able to clear the
    flag -- otherwise the note outlives the condition it describes."""
    scope = databases_scope()
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, [{"name": "ACME"}], None)
    repo.finish_pass(scope, generation, truncated=True)

    generation = repo.begin_pass(scope)
    assert not repo.entry(scope).truncated
    repo.write_page(scope, generation, [{"name": "ACME"}], None)
    repo.finish_pass(scope, generation)
    assert not repo.entry(scope).truncated


def test_a_data_preview_reads_back_in_row_order_and_a_shorter_pass_sweeps(
    repo,
) -> None:
    """Rows are keyed by position, so a second, shorter pass overwrites the
    positions it has and the sweep drops the rest."""
    scope = data_preview_scope("MYDB", "PUBLIC", "ORDERS")
    load(
        repo,
        scope,
        [{"_row_number": n, "_values": f'{{"ID": {n}}}'} for n in (2, 0, 1)],
    )
    assert [r["_values"] for r in repo.raw_rows(scope)] == [
        '{"ID": 0}',
        '{"ID": 1}',
        '{"ID": 2}',
    ]
    load(repo, scope, [{"_row_number": 0, "_values": '{"ID": 9}'}])
    assert [r["_values"] for r in repo.raw_rows(scope)] == ['{"ID": 9}']


def _preview_rows(*ids: int) -> list[dict]:
    return [{"_row_number": n, "_values": f'{{"ID": {i}}}'} for n, i in enumerate(ids)]


def test_a_data_preview_never_reaches_the_cache_file(tmp_path) -> None:
    """Table data, possibly personal data: neither its rows nor its ledger
    entry may be written to disk, while every other scope still is."""
    import sqlite3

    path = tmp_path / "cache.db"
    database = Database(path)
    assert database.prepare()
    repo = Repository(database)
    preview = data_preview_scope("MYDB", "PUBLIC", "ORDERS")
    load(repo, preview, _preview_rows(1, 2))
    load(repo, databases_scope(), db_rows("MYDB"))
    assert len(repo.raw_rows(preview)) == 2
    assert repo.entry(preview).state is CacheState.COMPLETE

    connection = sqlite3.connect(path)
    try:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master")]
        scopes = [r[0] for r in connection.execute("SELECT scope FROM cache_entry")]
    finally:
        connection.close()
    assert "data_preview" not in tables
    assert scopes == ["databases"]


def test_a_new_repository_on_the_same_file_starts_with_no_previews(tmp_path) -> None:
    """What a reconnect or a role switch back to this file opens."""
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    preview = data_preview_scope("MYDB", "PUBLIC", "ORDERS")
    load(Repository(database), preview, _preview_rows(1))
    again = Repository(database)
    assert again.raw_rows(preview) == []
    assert again.entry(preview).state is CacheState.EMPTY


def test_a_preview_written_on_a_worker_thread_reads_back_on_another(repo) -> None:
    """The memory store holds one connection, shared by the fetch workers and
    the event loop -- sqlite refuses that unless told not to check."""
    import threading

    preview = data_preview_scope("MYDB", "PUBLIC", "ORDERS")
    worker = threading.Thread(target=load, args=(repo, preview, _preview_rows(7)))
    worker.start()
    worker.join()
    assert [r["_values"] for r in repo.raw_rows(preview)] == ['{"ID": 7}']


def test_the_sweep_judges_previews_and_cached_scopes_from_their_own_stores(
    repo,
) -> None:
    preview = data_preview_scope("MYDB", "PUBLIC", "ORDERS")
    fresh_preview = data_preview_scope("MYDB", "PUBLIC", "CUSTOMERS")
    load(repo, databases_scope(), db_rows("MYDB"))
    load(repo, fresh_preview, _preview_rows(1))
    stale = repo.stale_scopes(
        3600, [databases_scope(), preview, fresh_preview, schemas_scope("MYDB")]
    )
    assert stale == [preview, schemas_scope("MYDB")]


# ------------------------------------------------------------ window passes

#: A fixed offset other than UTC, so a test sees the stored text and the
#: instant it means disagree, the way Snowflake's session offset makes them.
_PACIFIC = timezone(timedelta(hours=-8))


def _at(epoch: float, tz: timezone = _PACIFIC) -> str:
    """An epoch the way a Snowflake timestamp reaches the cache."""
    return str(datetime.fromtimestamp(epoch, tz))


def _load_row(pipe: str, at: float, file: str = "a.csv") -> dict:
    return {
        "PIPE_CATALOG_NAME": "MYDB",
        "PIPE_SCHEMA_NAME": "PUBLIC",
        "PIPE_NAME": pipe,
        "FILE_NAME": file,
        "LAST_LOAD_TIME": _at(at),
        "TABLE_CATALOG_NAME": "MYDB",
        "TABLE_SCHEMA_NAME": "PUBLIC",
        "TABLE_NAME": "ORDERS",
    }


def _full_pass(repo, scope, rows, covered_from: float = 0.0) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation, covered_from=covered_from)


def _window_pass(repo, scope, rows, prune: Prune, *, truncated=False) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation, truncated=truncated, prune=prune)


def _loads(repo, scope) -> list[tuple[str, str]]:
    return sorted((row["PIPE_NAME"], row["FILE_NAME"]) for row in repo.raw_rows(scope))


_DAY = 86400.0
_LONG_AGO = Prune(cutoff=0.0)


def test_a_window_pass_keeps_every_row_its_window_did_not_return(repo) -> None:
    """A window asks only for what is new, so a pipe that did not load in it
    keeps its last load -- a full pass would have swept it."""
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    now = time.time()
    _full_pass(
        repo, scope, [_load_row("P_A", now - _DAY), _load_row("P_B", now - _DAY)]
    )
    _window_pass(repo, scope, [_load_row("P_C", now)], _LONG_AGO)
    assert _loads(repo, scope) == [
        ("P_A", "a.csv"),
        ("P_B", "a.csv"),
        ("P_C", "a.csv"),
    ]


def test_a_window_pass_keeps_only_the_newest_load_per_pipe(repo) -> None:
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    now = time.time()
    _full_pass(repo, scope, [_load_row("P_A", now - _DAY, "old.csv")])
    _window_pass(repo, scope, [_load_row("P_A", now, "new.csv")], _LONG_AGO)
    assert _loads(repo, scope) == [("P_A", "new.csv")]


def test_an_overlap_read_again_does_not_replace_a_newer_load(repo) -> None:
    """The window reaches back before the previous pass, so it can return a
    load older than one already stored."""
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    now = time.time()
    _full_pass(repo, scope, [_load_row("P_A", now, "new.csv")])
    _window_pass(repo, scope, [_load_row("P_A", now - 3600, "old.csv")], _LONG_AGO)
    assert _loads(repo, scope) == [("P_A", "new.csv")]


def test_newer_is_judged_by_the_instant_not_by_the_stored_text(repo) -> None:
    """10:00 at -08:00 is 18:00 UTC, later than 12:00 UTC, though its text
    sorts first."""
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    west = {
        **_load_row("P_A", 0, "west.csv"),
        "LAST_LOAD_TIME": "2026-01-01 10:00:00-08:00",
    }
    utc = {
        **_load_row("P_A", 0, "utc.csv"),
        "LAST_LOAD_TIME": "2026-01-01 12:00:00+00:00",
    }
    _full_pass(repo, scope, [utc])
    _window_pass(repo, scope, [west], _LONG_AGO)
    assert _loads(repo, scope) == [("P_A", "west.csv")]


def test_julianday_reads_both_stored_timestamp_forms() -> None:
    """The sqlite3 adapter writes fractional seconds only when there are some;
    both forms, with an offset, must compare as instants."""
    import sqlite3

    connection = sqlite3.connect(":memory:")
    with_fraction, without = connection.execute(
        "SELECT julianday('2026-11-03 08:51:00.123000-08:00'), "
        "julianday('2026-11-03 08:51:00-08:00')"
    ).fetchone()
    utc = connection.execute(
        "SELECT julianday('2026-11-03 16:51:00.123+00:00')"
    ).fetchone()[0]
    assert with_fraction == pytest.approx(utc)
    assert with_fraction > without


def test_a_window_pass_prunes_rows_older_than_the_fetch_window(repo) -> None:
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    now = time.time()
    _full_pass(
        repo,
        scope,
        [_load_row("P_OLD", now - 100 * _DAY), _load_row("P_NEW", now)],
    )
    _window_pass(repo, scope, [], Prune(cutoff=now - 90 * _DAY))
    assert _loads(repo, scope) == [("P_NEW", "a.csv")]


def test_a_capped_history_keeps_its_newest_rows_and_says_it_is_truncated(
    repo,
) -> None:
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", "P_A")
    now = time.time()
    hours_ago = (5, 4, 3)
    _full_pass(
        repo, scope, [_load_row("P_A", now - h * 3600, f"{h}.csv") for h in hours_ago]
    )
    new = [_load_row("P_A", now - h * 3600, f"{h}.csv") for h in (2, 1)]
    _window_pass(repo, scope, new, Prune(cutoff=0.0, max_rows=4))
    assert sorted(row["FILE_NAME"] for row in repo.raw_rows(scope)) == [
        "1.csv",
        "2.csv",
        "3.csv",
        "4.csv",
    ]
    assert repo.entry(scope).truncated


def test_a_window_pass_under_the_cap_is_not_truncated(repo) -> None:
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", "P_A")
    now = time.time()
    _full_pass(repo, scope, [_load_row("P_A", now - 3600, "old.csv")])
    _window_pass(
        repo, scope, [_load_row("P_A", now, "new.csv")], Prune(0.0, max_rows=10)
    )
    entry = repo.entry(scope)
    assert not entry.truncated
    assert entry.row_count == 2


def test_a_window_pass_that_hit_its_cap_sweeps_like_a_full_pass(repo) -> None:
    """Its rows are the newest overall; older stored rows would sit behind a
    gap of rows it was not allowed to return."""
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", "P_A")
    now = time.time()
    _full_pass(repo, scope, [_load_row("P_A", now - _DAY, "old.csv")])
    _window_pass(
        repo,
        scope,
        [_load_row("P_A", now, "new.csv")],
        Prune(0.0, max_rows=1),
        truncated=True,
    )
    assert [row["FILE_NAME"] for row in repo.raw_rows(scope)] == ["new.csv"]
    assert repo.entry(scope).truncated


def test_a_window_pass_keeps_the_coverage_its_full_pass_recorded(repo) -> None:
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    _full_pass(repo, scope, [], covered_from=1234.0)
    _window_pass(repo, scope, [], _LONG_AGO)
    assert repo.entry(scope).covered_from == 1234.0


def test_forgetting_coverage_makes_the_next_pass_full(repo) -> None:
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    now = time.time()
    _full_pass(repo, scope, [], covered_from=now - 90 * _DAY)
    repo.forget_coverage(scope)
    entry = repo.entry(scope)
    assert entry.covered_from is None
    assert plan_window(entry, int(90 * _DAY), 7200, now) == Window(int(90 * _DAY), True)


def test_forgetting_coverage_keeps_the_rows_on_screen(repo) -> None:
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    _full_pass(repo, scope, [_load_row("P_A", time.time())])
    repo.forget_coverage(scope)
    assert _loads(repo, scope) == [("P_A", "a.csv")]


def test_a_failed_window_pass_deletes_nothing(repo) -> None:
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    _full_pass(repo, scope, [_load_row("P_A", time.time())])
    generation = repo.begin_pass(scope)
    repo.fail_pass(scope, generation, "no warehouse")
    assert _loads(repo, scope) == [("P_A", "a.csv")]


def _run(task: str, scheduled: float, completed: float, query_id=None) -> dict:
    return {
        "NAME": task,
        "DATABASE_NAME": "MYDB",
        "SCHEMA_NAME": "PUBLIC",
        "QUERY_ID": query_id,
        "SCHEDULED_TIME": _at(scheduled),
        "ATTEMPT_NUMBER": 1,
        "COMPLETED_TIME": _at(completed),
        "STATE": "FAILED" if query_id is None else "SUCCEEDED",
    }


def test_reading_an_overlap_again_adds_no_duplicate_task_run(repo) -> None:
    """A run that failed before a query was submitted has no QUERY_ID, which
    is why a run is keyed by SCHEDULED_TIME and ATTEMPT_NUMBER instead."""
    scope = task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS")
    now = time.time()
    run = _run("REFRESH_ORDERS", now - 600, now - 590)
    _full_pass(repo, scope, [run])
    _window_pass(repo, scope, [run], _LONG_AGO)
    _window_pass(repo, scope, [run], _LONG_AGO)
    assert len(repo.raw_rows(scope)) == 1


def test_a_task_run_without_a_query_id_can_be_its_latest_run(repo) -> None:
    """The schema-wide table used to require a QUERY_ID, which such a run
    does not have."""
    scope = task_activity_scope("MYDB", "PUBLIC")
    now = time.time()
    load(repo, scope, [_run("REFRESH_ORDERS", now - 600, now - 590)])
    assert [row["STATE"] for row in repo.raw_rows(scope)] == ["FAILED"]


def test_a_task_keeps_only_its_newest_run(repo) -> None:
    scope = task_activity_scope("MYDB", "PUBLIC")
    now = time.time()
    _full_pass(repo, scope, [_run("T", now - 7200, now - 7100, "old")])
    _window_pass(repo, scope, [_run("T", now - 600, now - 500, "new")], _LONG_AGO)
    assert [row["QUERY_ID"] for row in repo.raw_rows(scope)] == ["new"]


def test_a_task_is_pruned_by_when_its_run_completed(repo) -> None:
    """The window filters on COMPLETED_TIME, so pruning does too: a run
    scheduled before the cutoff but finished after it is inside the window."""
    scope = task_activity_scope("MYDB", "PUBLIC")
    now = time.time()
    _full_pass(repo, scope, [_run("LONG_TASK", now - 91 * _DAY, now - 89 * _DAY, "q")])
    _window_pass(repo, scope, [], Prune(cutoff=now - 90 * _DAY))
    assert [row["NAME"] for row in repo.raw_rows(scope)] == ["LONG_TASK"]


# ------------------------------------------------------------- plan_window


def _entry(**fields) -> Entry:
    defaults = {
        "scope_key": "pipe_activity:MYDB.PUBLIC",
        "state": CacheState.COMPLETE,
        "generation": 1,
        "fetched_at": None,
        "row_count": 0,
        "error": None,
    }
    return Entry(**{**defaults, **fields})


_FETCH = int(90 * _DAY)
_NOW = 1_800_000_000.0


def test_the_first_pass_of_a_log_scope_is_a_full_pass() -> None:
    entry = _entry(state=CacheState.EMPTY)
    assert plan_window(entry, _FETCH, 7200, _NOW) == Window(_FETCH, True)


def test_a_later_pass_reaches_back_to_the_last_one_minus_the_margin() -> None:
    entry = _entry(fetched_at=_NOW - 300, covered_from=_NOW - _FETCH - 300)
    assert plan_window(entry, _FETCH, 7200, _NOW) == Window(7500, False)


def test_a_failed_refresh_leaves_the_window_reaching_back_to_the_last_success() -> None:
    entry = _entry(
        state=CacheState.ERROR,
        fetched_at=_NOW - 3600,
        covered_from=_NOW - _FETCH - 3600,
    )
    assert plan_window(entry, _FETCH, 7200, _NOW) == Window(10800, False)


def test_a_raised_fetch_window_forces_a_full_pass() -> None:
    entry = _entry(fetched_at=_NOW - 300, covered_from=_NOW - _FETCH - 300)
    raised = int(120 * _DAY)
    assert plan_window(entry, raised, 7200, _NOW) == Window(raised, True)


def test_a_window_as_long_as_the_fetch_window_is_a_full_pass() -> None:
    """After the app was closed for longer than the fetch window."""
    entry = _entry(fetched_at=_NOW - _FETCH, covered_from=_NOW - 2 * _FETCH)
    assert plan_window(entry, _FETCH, 7200, _NOW) == Window(_FETCH, True)


# ------------------------------------------------------- pipe target table


def _backfill_row(at: str, table: str | None) -> dict:
    return {
        "PIPE_CATALOG_NAME": "MYDB",
        "PIPE_SCHEMA_NAME": "PUBLIC",
        "PIPE_NAME": "LOAD_ORDERS",
        "FILE_NAME": f"{at}.csv",
        "LAST_LOAD_TIME": at,
        "TABLE_CATALOG_NAME": "MYDB" if table else None,
        "TABLE_SCHEMA_NAME": "RAW" if table else None,
        "TABLE_NAME": table,
    }


def test_pipe_target_table_falls_back_to_the_pipes_own_backfill(repo) -> None:
    """Before the schema-wide snapshot answers, the pipe's own backfill
    already carries Snowflake's resolution."""
    load(
        repo,
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
        [_backfill_row("2026-01-02 03:04:05+00:00", "ORDERS")],
    )
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS") == (
        "MYDB",
        "RAW",
        "ORDERS",
    )


def test_the_backfills_newest_load_names_the_target(repo) -> None:
    """A pipe re-created into another table: the newest load says where it
    loads now. 10:00 at -08:00 is later than 12:00 UTC, though its text
    sorts first; a load without a table is skipped."""
    load(
        repo,
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
        [
            _backfill_row("2026-01-01 12:00:00+00:00", "OLD_ORDERS"),
            _backfill_row("2026-01-01 10:00:00-08:00", "ORDERS"),
            _backfill_row("2026-01-01 11:00:00-08:00", None),
        ],
    )
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS") == (
        "MYDB",
        "RAW",
        "ORDERS",
    )


def test_the_snapshot_names_the_target_before_the_backfill(repo) -> None:
    load(
        repo,
        pipe_activity_scope("MYDB", "PUBLIC"),
        [_backfill_row("2026-01-02 03:04:05+00:00", "FROM_SNAPSHOT")],
    )
    load(
        repo,
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
        [_backfill_row("2026-01-02 03:04:05+00:00", "FROM_BACKFILL")],
    )
    assert repo.pipe_target_table("MYDB", "PUBLIC", "LOAD_ORDERS")[2] == (
        "FROM_SNAPSHOT"
    )
