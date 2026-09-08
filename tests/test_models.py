from __future__ import annotations

from snowcone.models import (
    ObjectKind,
    ObjectRef,
    Scope,
    ScopeKind,
    columns_scope,
    databases_scope,
    grants_scope,
    objects_scope,
    pipe_activity_history_scope,
    pipe_activity_scope,
    pipe_history_scope,
    roles_scope,
    schemas_scope,
    scope_ttl,
    table_stats_scope,
    tag_references_scope,
    warehouses_scope,
)


def test_object_kind_plural_matches_snowflake_show_nouns() -> None:
    """The plural is dropped straight into `SHOW <plural>`, so a wrong one
    here becomes a SQL compilation error at runtime."""
    assert ObjectKind.TABLE.plural == "tables"
    assert ObjectKind.VIEW.plural == "views"
    assert ObjectKind.PIPE.plural == "pipes"
    assert ObjectKind.STAGE.plural == "stages"
    assert ObjectKind.TASK.plural == "tasks"


def test_object_kind_title_is_the_tree_category_label() -> None:
    assert ObjectKind.TABLE.title == "Tables"


def test_scope_keys_are_readable_and_distinct() -> None:
    assert databases_scope().key == "databases"
    assert schemas_scope("MYDB").key == "schemas:MYDB"
    assert objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC").key == "tables:MYDB.PUBLIC"
    assert objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC").key == "pipes:MYDB.PUBLIC"
    assert columns_scope("MYDB", "PUBLIC", "ORDERS").key == "columns:MYDB.PUBLIC.ORDERS"


def test_scopes_of_different_object_kinds_in_one_schema_do_not_collide() -> None:
    """Tables and views live in the same schema but refresh independently;
    sharing a scope key would make one overwrite the other's freshness."""
    tables = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    views = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    assert tables != views
    assert tables.key != views.key


def test_scope_is_hashable_so_it_can_key_dedup_and_routing_dicts() -> None:
    scopes = {databases_scope(), databases_scope(), schemas_scope("MYDB")}
    assert len(scopes) == 2


def test_scope_str_is_its_key() -> None:
    assert str(schemas_scope("MYDB")) == "schemas:MYDB"


def test_scope_is_orderable_so_it_can_break_priority_queue_ties() -> None:
    assert min([schemas_scope("B"), schemas_scope("A")]).database == "A"


def test_scope_kind_covers_every_key_shape() -> None:
    for kind in ScopeKind:
        assert kind.value


def test_object_ref_fqn() -> None:
    ref = ObjectRef(ObjectKind.TABLE, "MYDB", "PUBLIC", "ORDERS")
    assert ref.fqn == "MYDB.PUBLIC.ORDERS"


def test_scope_defaults_leave_unused_fields_none() -> None:
    scope = Scope(ScopeKind.DATABASES)
    assert scope.database is None
    assert scope.object_kind is None


def test_table_stats_scope_key() -> None:
    assert table_stats_scope("MYDB", "PUBLIC").key == "table_stats:MYDB.PUBLIC"


def test_table_stats_scope_carries_no_object_kind() -> None:
    """Regression: MainScreen.on_scope_needed upgrades any scope with both
    object_kind and database set into a whole-database SHOW TABLES prime,
    regardless of scope.kind -- setting object_kind here would silently
    misroute every ensure()-triggered fetch of this scope into that prime
    instead of the INFORMATION_SCHEMA SELECT this scope actually needs."""
    assert table_stats_scope("MYDB", "PUBLIC").object_kind is None


def test_pipe_activity_scope_key() -> None:
    assert pipe_activity_scope("MYDB", "PUBLIC").key == "pipe_activity:MYDB.PUBLIC"


def test_pipe_activity_scope_carries_no_object_kind() -> None:
    """Same regression as table_stats -- see that test's comment."""
    assert pipe_activity_scope("MYDB", "PUBLIC").object_kind is None


def test_pipe_activity_history_scope_key() -> None:
    assert (
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key
        == "pipe_activity_history:MYDB.PUBLIC.LOAD_ORDERS"
    )


def test_pipe_activity_history_scope_carries_no_object_kind() -> None:
    """Same regression as table_stats -- see that test's comment."""
    assert (
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").object_kind is None
    )


def test_pipe_history_scope_key() -> None:
    assert (
        pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key
        == "pipe_history:MYDB.PUBLIC.LOAD_ORDERS"
    )


def test_pipe_history_scope_carries_no_object_kind() -> None:
    """Same regression as table_stats -- see that test's comment."""
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").object_kind is None


def test_a_schema_level_grants_scope_and_a_database_level_grants_scope_have_different_keys() -> (
    None
):
    schema_level = grants_scope("MYDB", "PUBLIC")
    database_level = grants_scope("MYDB")
    assert schema_level.key == "grants:MYDB.PUBLIC"
    assert database_level.key == "grants:MYDB"
    assert schema_level != database_level


def test_a_schema_level_tag_references_scope_and_a_database_level_one_have_different_keys() -> (
    None
):
    schema_level = tag_references_scope("MYDB", "PUBLIC")
    database_level = tag_references_scope("MYDB")
    assert schema_level.key == "tag_references:MYDB.PUBLIC"
    assert database_level.key == "tag_references:MYDB"
    assert schema_level != database_level


def test_grants_and_tag_references_scopes_carry_no_object_kind() -> None:
    """Same regression as table_stats -- see that test's comment."""
    assert grants_scope("MYDB", "PUBLIC").object_kind is None
    assert grants_scope("MYDB").object_kind is None
    assert tag_references_scope("MYDB", "PUBLIC").object_kind is None
    assert tag_references_scope("MYDB").object_kind is None


def test_scope_ttl_leaves_an_ordinary_scope_untouched() -> None:
    """Almost every scope kind has no override -- the caller's own TTL wins,
    whatever it is."""
    assert scope_ttl(table_stats_scope("MYDB", "PUBLIC"), 900) == 900
    assert scope_ttl(databases_scope(), 0) == 0


def test_scope_ttl_never_shortens_the_callers_own_ttl() -> None:
    """PIPE_ACTIVITY's override is a floor, not a fixed value -- a caller
    that already asks for something slower must not be sped up."""
    generous = 30 * 86400
    assert scope_ttl(pipe_activity_scope("MYDB", "PUBLIC"), generous) == generous


def test_scope_ttl_extends_pipe_activitys_refresh_past_the_ordinary_cadence() -> None:
    """13 days, one short of INFORMATION_SCHEMA.COPY_HISTORY's 14-day
    retention -- see models.py's _TTL_OVERRIDES comment for the full
    reasoning (the two sources must jointly cover every day with no gap)."""
    ordinary = 900
    effective = scope_ttl(pipe_activity_scope("MYDB", "PUBLIC"), ordinary)
    assert effective > ordinary
    assert effective == 13 * 86400


def test_scope_ttl_extends_pipe_activity_historys_refresh_too() -> None:
    """Same override, same reasoning, for the per-pipe backfill scope."""
    ordinary = 900
    effective = scope_ttl(
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"), ordinary
    )
    assert effective > ordinary
    assert effective == 13 * 86400


def test_warehouses_and_roles_are_flat_scopes_like_databases() -> None:
    """Neither is scoped to a database, so -- like databases_scope -- there is
    exactly one of each, account-wide."""
    assert warehouses_scope().key == "warehouses"
    assert roles_scope().key == "roles"
    assert warehouses_scope() == warehouses_scope()
    assert warehouses_scope() != roles_scope()
