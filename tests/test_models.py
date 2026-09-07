from __future__ import annotations

from snowcone.models import (
    ObjectKind,
    ObjectRef,
    Scope,
    ScopeKind,
    columns_scope,
    databases_scope,
    objects_scope,
    pipe_activity_scope,
    roles_scope,
    schemas_scope,
    table_stats_scope,
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


def test_warehouses_and_roles_are_flat_scopes_like_databases() -> None:
    """Neither is scoped to a database, so -- like databases_scope -- there is
    exactly one of each, account-wide."""
    assert warehouses_scope().key == "warehouses"
    assert roles_scope().key == "roles"
    assert warehouses_scope() == warehouses_scope()
    assert warehouses_scope() != roles_scope()
