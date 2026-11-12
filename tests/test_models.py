# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import pytest

from nivaria.models import (
    ObjectRef,
    ObjectType,
    Scope,
    ScopeType,
    columns_scope,
    containing_scopes,
    data_preview_scope,
    database_roles_scope,
    databases_scope,
    due_in,
    grants_scope,
    is_stale,
    materialized_view_stats_scope,
    object_grants_scope,
    objects_scope,
    pipe_activity_history_scope,
    pipe_activity_scope,
    pipe_history_scope,
    prime_scope,
    roles_scope,
    schemas_scope,
    stage_stats_scope,
    table_stats_scope,
    tag_references_scope,
    task_activity_history_scope,
    task_activity_scope,
    task_history_scope,
    view_stats_scope,
    warehouses_scope,
)


def test_object_kind_plural_matches_snowflake_show_nouns() -> None:
    """The plural is dropped straight into `SHOW <plural>`, so a wrong one
    here becomes a SQL compilation error at runtime."""
    assert ObjectType.TABLE.plural == "tables"
    assert ObjectType.VIEW.plural == "views"
    assert ObjectType.PIPE.plural == "pipes"
    assert ObjectType.STAGE.plural == "stages"
    assert ObjectType.TASK.plural == "tasks"


def test_object_kind_title_is_the_tree_category_label() -> None:
    assert ObjectType.TABLE.title == "Tables"


def test_scope_keys_are_readable_and_distinct() -> None:
    assert databases_scope().key == "databases"
    assert schemas_scope("MYDB").key == "schemas:MYDB"
    assert objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC").key == "tables:MYDB.PUBLIC"
    assert objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC").key == "pipes:MYDB.PUBLIC"
    assert columns_scope("MYDB", "PUBLIC", "ORDERS").key == "columns:MYDB.PUBLIC.ORDERS"


def test_scopes_of_different_object_kinds_in_one_schema_do_not_collide() -> None:
    """Tables and views live in the same schema but refresh independently;
    sharing a scope key would make one overwrite the other's freshness."""
    tables = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    views = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
    assert tables != views
    assert tables.key != views.key


def test_scope_is_hashable_so_it_can_key_dedup_and_routing_dicts() -> None:
    scopes = {databases_scope(), databases_scope(), schemas_scope("MYDB")}
    assert len(scopes) == 2


def test_scope_str_is_its_key() -> None:
    assert str(schemas_scope("MYDB")) == "schemas:MYDB"


def test_scope_kind_covers_every_key_shape() -> None:
    for object_type in ScopeType:
        assert object_type.value


def test_object_ref_fqn() -> None:
    ref = ObjectRef(ObjectType.TABLE, "MYDB", "PUBLIC", "ORDERS")
    assert ref.fqn == "MYDB.PUBLIC.ORDERS"


def test_scope_defaults_leave_unused_fields_none() -> None:
    scope = Scope(ScopeType.DATABASES)
    assert scope.database is None
    assert scope.object_type is None


def test_table_stats_scope_key() -> None:
    assert table_stats_scope("MYDB", "PUBLIC").key == "table_stats:MYDB.PUBLIC"


def test_view_stats_scope_key() -> None:
    assert view_stats_scope("MYDB", "PUBLIC").key == "view_stats:MYDB.PUBLIC"


def test_materialized_view_stats_scope_key() -> None:
    assert (
        materialized_view_stats_scope("MYDB", "PUBLIC").key
        == "materialized_view_stats:MYDB.PUBLIC"
    )


def test_stage_stats_scope_key() -> None:
    assert stage_stats_scope("MYDB", "PUBLIC").key == "stage_stats:MYDB.PUBLIC"


def test_pipe_activity_scope_key() -> None:
    assert pipe_activity_scope("MYDB", "PUBLIC").key == "pipe_activity:MYDB.PUBLIC"


def test_pipe_activity_history_scope_key() -> None:
    assert (
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key
        == "pipe_activity_history:MYDB.PUBLIC.LOAD_ORDERS"
    )


def test_pipe_history_scope_key() -> None:
    assert (
        pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key
        == "pipe_history:MYDB.PUBLIC.LOAD_ORDERS"
    )


def test_task_activity_scope_key() -> None:
    assert task_activity_scope("MYDB", "PUBLIC").key == "task_activity:MYDB.PUBLIC"


def test_task_activity_history_scope_key() -> None:
    assert (
        task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS").key
        == "task_activity_history:MYDB.PUBLIC.REFRESH_ORDERS"
    )


def test_task_history_scope_key() -> None:
    assert (
        task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS").key
        == "task_history:MYDB.PUBLIC.REFRESH_ORDERS"
    )


def test_a_schema_level_grants_scope_and_a_database_level_grants_scope_have_different_keys() -> (
    None
):
    schema_level = grants_scope("MYDB", "PUBLIC")
    database_level = grants_scope("MYDB")
    assert schema_level.key == "grants:MYDB.PUBLIC"
    assert database_level.key == "grants:MYDB"
    assert schema_level != database_level


def test_an_object_level_grants_scope_is_keyed_by_its_type_and_name() -> None:
    """A pipe may share a table's name, and each has grants of its own."""
    table = object_grants_scope(ObjectRef(ObjectType.TABLE, "MYDB", "PUBLIC", "X"))
    pipe = object_grants_scope(ObjectRef(ObjectType.PIPE, "MYDB", "PUBLIC", "X"))
    assert table.key == "grants:tables:MYDB.PUBLIC.X"
    assert pipe.key != table.key
    assert table.key != grants_scope("MYDB", "PUBLIC").key


def test_a_data_preview_scope_is_keyed_by_its_table() -> None:
    assert data_preview_scope("MYDB", "PUBLIC", "X").key == "data_preview:MYDB.PUBLIC.X"


def test_a_database_roles_scope_is_keyed_by_its_database() -> None:
    assert database_roles_scope("MYDB").key == "database_roles:MYDB"


def test_a_schema_level_tag_references_scope_and_a_database_level_one_have_different_keys() -> (
    None
):
    schema_level = tag_references_scope("MYDB", "PUBLIC")
    database_level = tag_references_scope("MYDB")
    assert schema_level.key == "tag_references:MYDB.PUBLIC"
    assert database_level.key == "tag_references:MYDB"
    assert schema_level != database_level


@pytest.mark.parametrize(
    "scope",
    [
        table_stats_scope("MYDB", "PUBLIC"),
        view_stats_scope("MYDB", "PUBLIC"),
        materialized_view_stats_scope("MYDB", "PUBLIC"),
        stage_stats_scope("MYDB", "PUBLIC"),
        pipe_activity_scope("MYDB", "PUBLIC"),
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
        pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
        task_activity_scope("MYDB", "PUBLIC"),
        task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS"),
        task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS"),
        grants_scope("MYDB", "PUBLIC"),
        grants_scope("MYDB"),
        tag_references_scope("MYDB", "PUBLIC"),
        tag_references_scope("MYDB"),
        database_roles_scope("MYDB"),
    ],
    ids=lambda scope: scope.key,
)
def test_only_an_objects_listing_carries_an_object_type(scope) -> None:
    """object_type says which object type an OBJECTS listing or
    DATABASE_PRIME is for, or which kind of object an object-level GRANTS
    scope names. Every scope here narrows by database/schema/table instead,
    so it stays unset."""
    assert scope.object_type is None


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (databases_scope(), []),
        (warehouses_scope(), []),
        (schemas_scope("MYDB"), [databases_scope()]),
        (grants_scope("MYDB"), [databases_scope()]),
        (database_roles_scope("MYDB"), [databases_scope()]),
        (prime_scope(ObjectType.TABLE, "MYDB"), [databases_scope()]),
        (grants_scope("MYDB", "S"), [schemas_scope("MYDB")]),
        (tag_references_scope("MYDB", "S"), [schemas_scope("MYDB")]),
        (objects_scope(ObjectType.PIPE, "MYDB", "S"), [schemas_scope("MYDB")]),
        (table_stats_scope("MYDB", "S"), [schemas_scope("MYDB")]),
        (stage_stats_scope("MYDB", "S"), [schemas_scope("MYDB")]),
        (
            columns_scope("MYDB", "S", "T"),
            [
                objects_scope(ObjectType.TABLE, "MYDB", "S"),
                objects_scope(ObjectType.VIEW, "MYDB", "S"),
            ],
        ),
        # Its statement names the pipe's target table: the pipe list has
        # nothing to drop.
        (pipe_history_scope("MYDB", "S", "P"), []),
        (
            object_grants_scope(ObjectRef(ObjectType.TABLE, "MYDB", "S", "T")),
            [objects_scope(ObjectType.TABLE, "MYDB", "S")],
        ),
        (
            task_activity_history_scope("MYDB", "S", "T"),
            [objects_scope(ObjectType.TASK, "MYDB", "S")],
        ),
        (
            data_preview_scope("MYDB", "S", "T"),
            [
                objects_scope(ObjectType.TABLE, "MYDB", "S"),
                objects_scope(ObjectType.VIEW, "MYDB", "S"),
            ],
        ),
    ],
    ids=str,
)
def test_a_vanished_object_is_cleared_by_refreshing_the_listing_one_level_up(
    scope, expected
) -> None:
    assert containing_scopes(scope) == expected


def test_every_log_scope_ages_on_the_ordinary_cache_age() -> None:
    """No scope is refreshed less often to save an expensive statement: a log
    scope is made cheap to refresh instead (repository.plan_window)."""
    assert due_in(100.0, 300) == 200.0
    assert is_stale(301.0, 300)
    assert not is_stale(299.0, 300)


def test_a_zero_cache_age_disables_refreshing_but_not_the_first_fetch() -> None:
    assert due_in(100.0, 0) is None
    assert due_in(None, 0) == 0.0


def test_warehouses_and_roles_are_flat_scopes_like_databases() -> None:
    """Neither is scoped to a database, so -- like databases_scope -- there is
    exactly one of each, account-wide."""
    assert warehouses_scope().key == "warehouses"
    assert roles_scope().key == "roles"
    assert warehouses_scope() == warehouses_scope()
    assert warehouses_scope() != roles_scope()
