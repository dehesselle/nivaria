"""The detail pane and its view registry."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static, TabbedContent, TextArea

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    SchemaRef,
    columns_scope,
    grants_scope,
    materialized_view_stats_scope,
    objects_scope,
    pipe_activity_history_scope,
    pipe_activity_scope,
    pipe_history_scope,
    table_stats_scope,
    tag_references_scope,
    task_activity_history_scope,
    task_activity_scope,
    task_history_scope,
    view_stats_scope,
)
from snowcone.settings import PIPE_HISTORY_SHOW_DAYS, TASK_HISTORY_SHOW_DAYS, config
from snowcone.ui.messages import ObjectSelected, ScopeNeeded
from snowcone.ui.views.base import registered_kinds, view_for
from snowcone.ui.views.pipe_list_view import PipeListView
from snowcone.ui.views.pipe_view import PipeView
from snowcone.ui.views.schema_view import DatabaseView, SchemaView
from snowcone.ui.views.table_list_view import TableListView
from snowcone.ui.views.table_view import TableView
from snowcone.ui.views.task_list_view import TaskListView
from snowcone.ui.views.task_view import TaskView
from snowcone.ui.views.view_list_view import ViewListView
from snowcone.ui.views.view_view import ViewView
from snowcone.ui.widgets.detail_pane import DetailPane


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self._repo = repo
        self.needed: list[str] = []
        self.selected: list[ObjectRef] = []

    def compose(self) -> ComposeResult:
        yield DetailPane(self._repo, id="detail")

    def on_scope_needed(self, event: ScopeNeeded) -> None:
        self.needed.append(event.scope.key)

    def on_object_selected(self, event: ObjectSelected) -> None:
        self.selected.append(event.ref)


def load(repo: Repository, scope, rows) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


def put_table(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "kind": "TABLE",
        "comment": "",
        "rows": 1234,
        "bytes": 4096,
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectKind.TABLE, "MYDB", "PUBLIC", name)


def put_columns(repo: Repository, table: str, *columns: tuple) -> None:
    """Each column is (name, data_type) or (name, data_type, extra), where
    extra is a dict of additional INFORMATION_SCHEMA fields, e.g. precision."""
    rows = []
    for position, column in enumerate(columns, start=1):
        name, data_type, *rest = column
        row = {
            "TABLE_CATALOG": "MYDB",
            "TABLE_SCHEMA": "PUBLIC",
            "TABLE_NAME": table,
            "COLUMN_NAME": name,
            "ORDINAL_POSITION": position,
            "DATA_TYPE": data_type,
            "IS_NULLABLE": "YES",
            "COLUMN_DEFAULT": None,
            "COMMENT": "",
        }
        if rest:
            row.update(rest[0])
        rows.append(row)
    load(repo, columns_scope("MYDB", "PUBLIC", table), rows)


def put_table_stats(repo: Repository, table: str, **extra) -> None:
    row = {
        "TABLE_CATALOG": "MYDB",
        "TABLE_SCHEMA": "PUBLIC",
        "TABLE_NAME": table,
        "LAST_DDL": "2026-01-01 00:00:00",
        "LAST_ALTERED": "2026-01-02 00:00:00",
    }
    row.update(extra)
    load(repo, table_stats_scope("MYDB", "PUBLIC"), [row])


def put_view(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "comment": "",
        "text": "SELECT * FROM t",
        "is_secure": "false",
        "is_materialized": "false",
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectKind.VIEW, "MYDB", "PUBLIC", name)


def put_view_stats(repo: Repository, view: str, **extra) -> None:
    row = {
        "TABLE_CATALOG": "MYDB",
        "TABLE_SCHEMA": "PUBLIC",
        "TABLE_NAME": view,
        "LAST_DDL": "2026-01-01 00:00:00",
        "LAST_ALTERED": "2026-01-02 00:00:00",
    }
    row.update(extra)
    load(repo, view_stats_scope("MYDB", "PUBLIC"), [row])


def put_materialized_view_stats(repo: Repository, view: str, **extra) -> None:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": view,
        "rows": 42,
        "bytes": 8192,
    }
    row.update(extra)
    load(repo, materialized_view_stats_scope("MYDB", "PUBLIC"), [row])


def put_pipe(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "definition": "COPY INTO t FROM @s",
        "owner": "LOADER",
        "notification_channel": "arn:sqs:...",
        "comment": "",
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectKind.PIPE, "MYDB", "PUBLIC", name)


def put_pipe_activity(repo: Repository, pipe: str, **extra) -> None:
    row = {
        "PIPE_CATALOG_NAME": "MYDB",
        "PIPE_SCHEMA_NAME": "PUBLIC",
        "PIPE_NAME": pipe,
        "FILE_NAME": "orders_1.csv.gz",
        "FILE_SIZE": 1024,
        "ROW_COUNT": 42,
        "STATUS": "Loaded",
        "LAST_LOAD_TIME": "2026-01-02 03:04:05",
        "TABLE_CATALOG_NAME": "MYDB",
        "TABLE_SCHEMA_NAME": "PUBLIC",
        "TABLE_NAME": "ORDERS",
    }
    row.update(extra)
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_pipe_activity_history(repo: Repository, pipe: str, **extra) -> None:
    """Seeds a single pipe's ACCOUNT_USAGE backfill scope -- what PipeView's
    Load History tab actually merges, unlike the schema-wide put_pipe_activity
    above (which PipeListView reads, and which PipeView only reads for target
    resolution now)."""
    row = {
        "PIPE_CATALOG_NAME": "MYDB",
        "PIPE_SCHEMA_NAME": "PUBLIC",
        "PIPE_NAME": pipe,
        "FILE_NAME": "orders_1.csv.gz",
        "FILE_SIZE": 1024,
        "ROW_COUNT": 42,
        "STATUS": "Loaded",
        "LAST_LOAD_TIME": "2026-01-02 03:04:05",
        "TABLE_CATALOG_NAME": "MYDB",
        "TABLE_SCHEMA_NAME": "PUBLIC",
        "TABLE_NAME": "ORDERS",
    }
    row.update(extra)
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", pipe)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_pipe_history(repo: Repository, pipe: str, **extra) -> None:
    row = {
        "FILE_NAME": "orders_9.csv.gz",
        "FILE_SIZE": 2048,
        "ROW_COUNT": 100,
        "STATUS": "Loaded",
        "LAST_LOAD_TIME": "2026-03-01 08:00:00",
        "PIPE_NAME": pipe,
    }
    row.update(extra)
    scope = pipe_history_scope("MYDB", "PUBLIC", pipe)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_task(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "definition": "INSERT INTO t SELECT * FROM s",
        "owner": "LOADER",
        "warehouse": "MYWH",
        "schedule": "5 MINUTE",
        "state": "started",
        "comment": "",
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectKind.TASK, "MYDB", "PUBLIC", name)


def put_task_activity(repo: Repository, task: str, **extra) -> None:
    row = {
        "NAME": task,
        "DATABASE_NAME": "MYDB",
        "SCHEMA_NAME": "PUBLIC",
        "QUERY_ID": "01ab-query-id",
        "SCHEDULED_TIME": "2026-01-02 03:04:05",
        "QUERY_START_TIME": "2026-01-02 03:04:06",
        "COMPLETED_TIME": "2026-01-02 03:04:10",
        "STATE": "SUCCEEDED",
    }
    row.update(extra)
    scope = task_activity_scope("MYDB", "PUBLIC")
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_task_activity_history(repo: Repository, task: str, **extra) -> None:
    """Seeds a single task's ACCOUNT_USAGE backfill scope -- what TaskView's
    Run History tab actually merges, unlike the schema-wide put_task_activity
    above (which TaskListView reads)."""
    row = {
        "NAME": task,
        "DATABASE_NAME": "MYDB",
        "SCHEMA_NAME": "PUBLIC",
        "QUERY_ID": "01ab-query-id",
        "SCHEDULED_TIME": "2026-01-02 03:04:05",
        "QUERY_START_TIME": "2026-01-02 03:04:06",
        "COMPLETED_TIME": "2026-01-02 03:04:10",
        "STATE": "SUCCEEDED",
    }
    row.update(extra)
    scope = task_activity_history_scope("MYDB", "PUBLIC", task)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_task_history(repo: Repository, task: str, **extra) -> None:
    row = {
        "NAME": task,
        "QUERY_ID": "02cd-query-id",
        "SCHEDULED_TIME": "2026-03-01 08:00:00",
        "QUERY_START_TIME": "2026-03-01 08:00:01",
        "COMPLETED_TIME": "2026-03-01 08:00:05",
        "STATE": "SUCCEEDED",
    }
    row.update(extra)
    scope = task_history_scope("MYDB", "PUBLIC", task)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_grant(repo: Repository, database: str, schema: str | None, **extra) -> None:
    row = {
        "created_on": "2026-01-01 00:00:00",
        "privilege": "USAGE",
        "granted_on": "SCHEMA" if schema is not None else "DATABASE",
        "name": f"{database}.{schema}" if schema is not None else database,
        "granted_to": "ROLE",
        "grantee_name": "ANALYST",
        "grant_option": "false",
        "granted_by": "SYSADMIN",
    }
    row.update(extra)
    scope = grants_scope(database, schema)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_tag(repo: Repository, database: str, schema: str | None, **extra) -> None:
    row = {
        "TAG_DATABASE": database,
        "TAG_SCHEMA": "TAGS",
        "TAG_NAME": "COST_CENTER",
        "TAG_VALUE": "ENG",
        "DOMAIN": "SCHEMA" if schema is not None else "DATABASE",
    }
    row.update(extra)
    scope = tag_references_scope(database, schema)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


# ---------------------------------------------------------------- registry


def test_every_kind_the_tree_shows_has_a_view_or_is_known_missing() -> None:
    """A kind with no view is a placeholder, not a crash -- but the ones that
    do have views must actually be registered by importing their modules."""
    assert view_for(ObjectKind.TABLE) is TableView
    assert view_for(ObjectKind.VIEW) is ViewView
    assert view_for(ObjectKind.PIPE) is PipeView
    assert view_for(ObjectKind.TASK) is TaskView


def test_registration_is_by_object_kind() -> None:
    assert {
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.PIPE,
        ObjectKind.TASK,
    } <= registered_kinds()


def test_registering_a_duplicate_kind_is_refused() -> None:
    """Two views for one kind means one silently wins; better to fail loudly
    at import time than to wonder which is showing."""
    from snowcone.ui.views.base import ObjectView, register_view

    with pytest.raises(ValueError):

        @register_view
        class Duplicate(ObjectView):
            OBJECT_KIND = ObjectKind.TABLE

            def update_content(self) -> None:
                pass


# ------------------------------------------------------------------ swapping


async def test_showing_a_table_mounts_the_table_view(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"), ("TOTAL", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert isinstance(pane.view, TableView)


async def test_moving_between_two_tables_reuses_the_view(repo) -> None:
    """The reason the pane swaps on kind rather than on object: arrowing down
    three hundred tables must not remount a widget three hundred times."""
    first = put_table(repo, "ORDERS")
    second = put_table(repo, "CUSTOMERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view_a = pane.show(first)
        await pilot.pause()
        view_b = pane.show(second)
        await pilot.pause()
        assert view_a is view_b
        assert pane.view.ref.name == "CUSTOMERS"


async def test_moving_to_a_different_kind_replaces_the_view(repo) -> None:
    table = put_table(repo, "ORDERS")
    pipe = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(table)
        await pilot.pause()
        pane.show(pipe)
        await pilot.pause()
        assert isinstance(pane.view, PipeView)
        assert len(pane.query(TableView)) == 0


async def test_an_unregistered_kind_shows_a_placeholder(repo) -> None:
    """A kind the tree can produce but nothing has a view for yet must not
    take the app down."""
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        result = pane.show(ObjectRef(ObjectKind.STAGE, "MYDB", "PUBLIC", "S1"))
        await pilot.pause()
        assert result is None
        assert pane.query_one("#detail-placeholder", Static)


async def test_clearing_empties_the_pane(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        pane.clear()
        await pilot.pause()
        assert pane.view is None
        assert pane.scopes() == []


# -------------------------------------------------------------- table view


async def test_the_table_view_lists_cached_columns(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"), ("TOTAL", "FLOAT"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-columns", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "ID"
        assert table.get_cell_at((1, 1)).plain == "FLOAT"


async def test_the_table_view_renders_full_ddl_style_type_names(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(
        repo,
        "ORDERS",
        ("TOTAL", "FIXED", {"NUMERIC_PRECISION": 38, "NUMERIC_SCALE": 0}),
        ("CREATED_AT", "TIMESTAMP_LTZ", {"DATETIME_PRECISION": 9}),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-columns", DataTable)
        assert table.get_cell_at((0, 1)).plain == "NUMBER(38,0)"
        assert table.get_cell_at((1, 1)).plain == "TIMESTAMP_LTZ(9)"


async def test_missing_columns_are_requested(repo) -> None:
    """The pane paints from the cache and asks for what is not there."""
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert "columns:MYDB.PUBLIC.ORDERS" in app.needed


async def test_cached_columns_are_not_re_requested(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert app.needed == []


async def test_a_cache_update_for_the_shown_table_repaints(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-columns", DataTable).row_count == 0

        put_columns(repo, "ORDERS", ("ID", "NUMBER"))
        pane.cache_updated(columns_scope("MYDB", "PUBLIC", "ORDERS"))
        await pilot.pause()
        assert pane.query_one("#detail-columns", DataTable).row_count == 1


async def test_an_unrelated_cache_update_is_ignored(repo) -> None:
    """Views subscribe to their own scopes, not to everything."""
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        put_columns(repo, "ORDERS", ("ID", "NUMBER"))
        pane.cache_updated(columns_scope("MYDB", "PUBLIC", "SOMETHING_ELSE"))
        await pilot.pause()
        assert pane.query_one("#detail-columns", DataTable).row_count == 0


async def test_the_pane_reports_the_scopes_it_reads(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert [s.key for s in pane.scopes()] == ["columns:MYDB.PUBLIC.ORDERS"]


# --------------------------------------------------------------- view view


async def test_the_view_columns_tab_is_active_by_default(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.view.query_one(TabbedContent).active == "tab-columns"


async def test_the_view_columns_tab_lists_cached_columns(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    put_columns(repo, "ORDERS_V", ("ID", "NUMBER"), ("TOTAL", "FLOAT"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-columns", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "ID"
        assert table.get_cell_at((1, 1)).plain == "FLOAT"


async def test_the_view_definition_tab_shows_the_views_sql_text(repo) -> None:
    ref = put_view(repo, "ORDERS_V", text="SELECT id, total FROM orders")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        text_area = pane.query_one("#view-definition", TextArea)
        assert text_area.text == "SELECT id, total FROM orders"


async def test_the_view_definition_tab_falls_back_when_nothing_is_cached(repo) -> None:
    ref = ObjectRef(ObjectKind.VIEW, "MYDB", "PUBLIC", "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        text_area = pane.query_one("#view-definition", TextArea)
        assert text_area.text == "-- not cached"


async def test_the_view_tab_mnemonic_actions_switch_tabs(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        view = pane.view
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-columns"
        view.action_show_tab("definition")
        assert tabs.active == "tab-definition"
        view.action_show_tab("columns")
        assert tabs.active == "tab-columns"


# --------------------------------------------------------------- pipe view


async def test_a_pipe_shows_load_history_not_columns(repo) -> None:
    """A different shape entirely -- which is what the registry is for."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable)
        assert len(pane.query("#detail-columns")) == 0


async def test_opening_a_pipe_requests_its_activity_scope(repo) -> None:
    """The base facts came with the SHOW PIPES row already cached, but load
    history is backed by its own SELECT scope -- see pipe_view.py -- and is
    requested the moment the view opens, same as a table's columns scope."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert pipe_activity_scope("MYDB", "PUBLIC").key in app.needed


async def test_a_pipe_with_no_activity_data_never_requests_history_or_errors(
    repo,
) -> None:
    """A pipe that has never loaded anything has no cached activity data, so
    its target table cannot be resolved (Snowflake exposes it nowhere else --
    see Repository.pipe_target_table). That must not repeatedly fail a fetch
    it can never succeed at: history_scope is simply never requested until a
    target becomes resolvable, and the view shows an empty table instead of
    an error."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable).row_count == 0
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key not in app.needed


async def test_a_pipe_with_activity_data_requests_its_history_scope_too(repo) -> None:
    """Once account_usage_copy_history has a row for this pipe, its target
    table is resolvable and history_scope becomes worth asking for."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key in app.needed


async def test_a_pipes_history_table_is_empty_with_no_activity_or_history_cached(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable).row_count == 0


def _days_ago(days: float) -> str:
    """A relative, always-recent-enough timestamp -- these tests filter by
    the show window against the real current time, so a fixed calendar date
    would eventually age out of it."""
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat()


async def test_a_pipes_history_table_lists_rows_from_both_sources(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")  # resolves the target table
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=_days_ago(60),
        ROW_COUNT=42,
    )
    put_pipe_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_9.csv.gz",
        LAST_LOAD_TIME=_days_ago(5),
        ROW_COUNT=100,
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-history", DataTable)
        assert table.row_count == 2
        # Sorted newest first.
        assert table.get_cell_at((0, 4)).plain == "orders_9.csv.gz"
        assert table.get_cell_at((1, 4)).plain == "orders_1.csv.gz"


async def test_a_pipes_history_ignores_another_pipes_activity_rows(repo) -> None:
    """account_usage_copy_history_detail is scoped per pipe -- a row cached
    for another pipe must never leak into this one's history table."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity_history(repo, "LOAD_OTHER", FILE_NAME="other.csv.gz")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable).row_count == 0


async def test_the_near_real_time_source_wins_on_an_overlapping_row(repo) -> None:
    """Merged by (FILE_NAME, LAST_LOAD_TIME) -- the near-real-time source is
    inserted last, so an identical key from the backfill is overlaid rather
    than duplicated."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    when = _days_ago(10)
    put_pipe_activity(repo, "LOAD_ORDERS")  # resolves the target table
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=when,
        STATUS="Load in progress",
    )
    put_pipe_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=when,
        STATUS="Loaded",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-history", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 3)).plain == "Loaded"


async def test_rows_older_than_the_show_window_are_hidden_but_stay_cached(repo) -> None:
    """The display window can be shorter than the fetch window on purpose --
    shrinking it is a display-only change, since the wider range is still in
    the cache either way (see settings.PIPE_HISTORY_SHOW_DAYS)."""
    config.set(PIPE_HISTORY_SHOW_DAYS, 30)
    ref = put_pipe(repo, "LOAD_ORDERS")
    old = datetime.now().astimezone() - timedelta(days=31)
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="ancient.csv.gz",
        LAST_LOAD_TIME=old.isoformat(),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable).row_count == 0
    # Still in the cache -- only hidden from this paint.
    assert repo.raw_rows(pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"))


async def test_the_highlighted_rows_error_shows_in_the_history_detail_panel(
    repo,
) -> None:
    """Too long for a table column -- shown below the table instead, for
    whichever row the cursor is currently on."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        LAST_LOAD_TIME=_days_ago(1),
        FIRST_ERROR_MESSAGE="Numeric value 'x' is not recognized",
        FIRST_ERROR_LINE_NUMBER=42,
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#pipe-history-detail", Static)
        assert panel.display
        assert "Line 42" in str(panel.render())
        assert "Numeric value 'x' is not recognized" in str(panel.render())


async def test_a_row_with_no_error_leaves_the_history_detail_panel_hidden(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=_days_ago(1))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#pipe-history-detail", Static)
        assert not panel.display


async def test_highlighting_a_different_history_row_updates_the_detail_panel(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="bad.csv.gz",
        LAST_LOAD_TIME=_days_ago(1),
        FIRST_ERROR_MESSAGE="boom",
        FIRST_ERROR_LINE_NUMBER=1,
    )
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="good.csv.gz",
        LAST_LOAD_TIME=_days_ago(2),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-history", DataTable)
        panel = pane.query_one("#pipe-history-detail", Static)
        # Newest first: row 0 is "bad.csv.gz", the errored one.
        assert panel.display
        table.move_cursor(row=1)
        await pilot.pause()
        assert not panel.display
        table.move_cursor(row=0)
        await pilot.pause()
        assert panel.display


async def test_a_repaint_does_not_leave_a_stale_error_showing(repo) -> None:
    """A cache update while a row is highlighted must resync against
    whatever is actually on that row now, not trust the last message."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    when = _days_ago(1)
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        LAST_LOAD_TIME=when,
        FIRST_ERROR_MESSAGE="boom",
        FIRST_ERROR_LINE_NUMBER=1,
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history-detail", Static).display

        # Same file/time key, now with no error -- overwrites the row.
        put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=when)
        pane.cache_updated(pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"))
        await pilot.pause()
        assert not pane.query_one("#pipe-history-detail", Static).display


async def test_the_definition_tab_shows_the_pipes_raw_sql(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS", definition="COPY INTO t FROM @s")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert (
            pane.query_one("#pipe-definition", TextArea).text == "COPY INTO t FROM @s"
        )


async def test_the_definition_tab_falls_back_when_uncached(repo) -> None:
    ref = ObjectRef(ObjectKind.PIPE, "MYDB", "PUBLIC", "GHOST")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-definition", TextArea).text == "-- not cached"


async def test_the_tab_mnemonic_actions_switch_tabs(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-history"
        view.action_show_tab("definition")
        assert tabs.active == "tab-definition"
        view.action_show_tab("history")
        assert tabs.active == "tab-history"


def test_pipe_view_scopes_always_includes_activity(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    view = PipeView(repo, ref)
    assert pipe_activity_scope("MYDB", "PUBLIC") in view.scopes()


def test_pipe_view_scopes_excludes_history_until_a_target_is_resolvable(repo) -> None:
    """This is what keeps the background sweep (MainScreen._sweep, which
    watches self.detail.scopes()) from repeatedly retrying a fetch that has
    no target table to give -- not just the interactive ensure() path."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    view = PipeView(repo, ref)
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS") not in view.scopes()

    put_pipe_activity(repo, "LOAD_ORDERS")
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS") in view.scopes()


# --------------------------------------------------------------- task view


async def test_a_task_shows_run_history_not_columns(repo) -> None:
    """A different shape entirely -- which is what the registry is for."""
    ref = put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#task-history", DataTable)
        assert len(pane.query("#detail-columns")) == 0


async def test_opening_a_task_requests_both_history_scopes_unconditionally(
    repo,
) -> None:
    """Unlike PipeView, there is no target-table resolution gate here: both
    task_activity_history_scope and task_history_scope are requested the
    moment the view opens, since DATABASE_NAME/SCHEMA_NAME/TASK_NAME already
    come straight off this task's own ref."""
    ref = put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS").key in (
        app.needed
    )
    assert task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS").key in app.needed


async def test_a_tasks_history_table_is_empty_with_nothing_cached(repo) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#task-history", DataTable).row_count == 0


def _days_ago_iso(days: float) -> str:
    """Same relative-timestamp trick as pipe view's _days_ago -- these tests
    filter by the show window against the real current time."""
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat()


async def test_a_tasks_history_table_lists_rows_from_both_sources(repo) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-old",
        SCHEDULED_TIME=_days_ago_iso(60),
    )
    put_task_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-new",
        SCHEDULED_TIME=_days_ago_iso(5),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#task-history", DataTable)
        assert table.row_count == 2
        # Sorted newest first.
        assert table.get_cell_at((0, 3)).plain == "SUCCEEDED"


async def test_a_tasks_history_ignores_another_tasks_activity_rows(repo) -> None:
    """account_usage_task_history_detail is scoped per task -- a row cached
    for another task must never leak into this one's history table."""
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(repo, "REFRESH_OTHER", QUERY_ID="q-other")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#task-history", DataTable).row_count == 0


async def test_the_near_real_time_source_wins_on_an_overlapping_query_id(repo) -> None:
    """Merged by QUERY_ID alone -- the near-real-time source is inserted
    last, so an identical id from the backfill is overlaid rather than
    duplicated."""
    ref = put_task(repo, "REFRESH_ORDERS")
    when = _days_ago_iso(10)
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-1",
        SCHEDULED_TIME=when,
        STATE="EXECUTING",
    )
    put_task_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-1",
        SCHEDULED_TIME=when,
        STATE="SUCCEEDED",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#task-history", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 3)).plain == "SUCCEEDED"


async def test_rows_older_than_the_task_show_window_are_hidden_but_stay_cached(
    repo,
) -> None:
    """Same display-vs-fetch-window split as pipe view -- see settings.
    TASK_HISTORY_SHOW_DAYS."""
    config.set(TASK_HISTORY_SHOW_DAYS, 30)
    ref = put_task(repo, "REFRESH_ORDERS")
    old = datetime.now().astimezone() - timedelta(days=31)
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-ancient",
        SCHEDULED_TIME=old.isoformat(),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#task-history", DataTable).row_count == 0
    # Still in the cache -- only hidden from this paint.
    assert repo.raw_rows(
        task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS")
    )


async def test_the_highlighted_rows_error_shows_in_the_task_history_detail_panel(
    repo,
) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        SCHEDULED_TIME=_days_ago_iso(1),
        ERROR_CODE=100132,
        ERROR_MESSAGE="Numeric value 'x' is not recognized",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#task-history-detail", Static)
        assert panel.display
        assert "100132" in str(panel.render())
        assert "Numeric value 'x' is not recognized" in str(panel.render())


async def test_a_task_row_with_no_error_leaves_the_history_detail_panel_hidden(
    repo,
) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(repo, "REFRESH_ORDERS", SCHEDULED_TIME=_days_ago_iso(1))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#task-history-detail", Static)
        assert not panel.display


async def test_highlighting_a_different_task_history_row_updates_the_detail_panel(
    repo,
) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-bad",
        SCHEDULED_TIME=_days_ago_iso(1),
        ERROR_CODE=1,
        ERROR_MESSAGE="boom",
    )
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-good",
        SCHEDULED_TIME=_days_ago_iso(2),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#task-history", DataTable)
        panel = pane.query_one("#task-history-detail", Static)
        # Newest first: row 0 is "q-bad", the errored one.
        assert panel.display
        table.move_cursor(row=1)
        await pilot.pause()
        assert not panel.display
        table.move_cursor(row=0)
        await pilot.pause()
        assert panel.display


async def test_the_definition_tab_shows_the_tasks_raw_sql(repo) -> None:
    ref = put_task(repo, "REFRESH_ORDERS", definition="INSERT INTO t SELECT * FROM s")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert (
            pane.query_one("#task-definition", TextArea).text
            == "INSERT INTO t SELECT * FROM s"
        )


async def test_the_task_definition_tab_falls_back_when_uncached(repo) -> None:
    ref = ObjectRef(ObjectKind.TASK, "MYDB", "PUBLIC", "GHOST")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#task-definition", TextArea).text == "-- not cached"


async def test_the_task_tab_mnemonic_actions_switch_tabs(repo) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-history"
        view.action_show_tab("definition")
        assert tabs.active == "tab-definition"
        view.action_show_tab("history")
        assert tabs.active == "tab-history"


def test_task_view_scopes_always_includes_all_three_task_scopes(repo) -> None:
    """Unlike PipeView, nothing here is gated on a resolved target -- see the
    module docstring's reasoning."""
    ref = put_task(repo, "REFRESH_ORDERS")
    view = TaskView(repo, ref)
    assert task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS") in (
        view.scopes()
    )
    assert task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS") in view.scopes()


# --------------------------------------------------------------- table list


async def test_selecting_a_tables_category_mounts_the_table_list_view(repo) -> None:
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, TableListView)


async def test_the_table_list_view_lists_every_cached_table(repo) -> None:
    put_table(repo, "ORDERS", rows=42, bytes=8192, created_on="2026-01-01 00:00:00")
    put_table(repo, "CUSTOMERS", rows=7, bytes=1024, created_on="2026-02-02 00:00:00")
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "CUSTOMERS"
        assert table.get_cell_at((0, 1)).plain == "7"
        assert table.get_cell_at((1, 0)).plain == "ORDERS"
        # Truncated to the calendar day -- the full timestamp is a hover
        # tooltip now, see test_table_list_hover.py.
        assert table.get_cell_at((1, 3)).plain == "2026-01-01"


async def test_table_stats_columns_render_when_the_scope_has_data(repo) -> None:
    put_table(repo, "ORDERS", created_on="2026-01-01 00:00:00")
    put_table_stats(
        repo,
        "ORDERS",
        LAST_DDL="2026-03-01 08:00:00",
        LAST_ALTERED="2026-03-02 09:30:00",
    )
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 3)).plain == "2026-01-01"
        assert table.get_cell_at((0, 4)).plain == "2026-03-01"
        assert table.get_cell_at((0, 5)).plain == "2026-03-02 09:30"


async def test_table_stats_columns_are_blank_without_a_successful_stats_pass(
    repo,
) -> None:
    """A missing/failed stats scope must not block the SHOW-sourced columns:
    the row still renders, just with the two SELECT-sourced cells blank."""
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""


def test_table_list_view_scopes_includes_the_stats_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    view = TableListView(repo, scope)
    assert table_stats_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_table_row_selects_it(repo) -> None:
    """Reuses the exact path a search hit takes (ObjectSelected ->
    MainScreen._select) -- this only has to prove the row posts the message
    with the right ref; the reveal/show wiring itself is search's own,
    already-tested plumbing."""
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected == [ObjectRef(ObjectKind.TABLE, "MYDB", "PUBLIC", "ORDERS")]


async def test_moving_between_two_schemas_tables_reuses_the_list_view(repo) -> None:
    put_table(repo, "ORDERS")
    other_scope = objects_scope(ObjectKind.TABLE, "MYDB", "OTHER")
    load(
        repo,
        other_scope,
        [
            {
                "database_name": "MYDB",
                "schema_name": "OTHER",
                "name": "EVENTS",
                "kind": "TABLE",
                "comment": "",
                "rows": 1,
                "bytes": 1,
            }
        ],
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view_a = pane.show_category(objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC"))
        await pilot.pause()
        view_b = pane.show_category(other_scope)
        await pilot.pause()
        assert view_a is view_b
        assert pane.query_one("#detail-table-list", DataTable).row_count == 1


# ----------------------------------------------------------------- view list


async def test_selecting_a_views_category_mounts_the_view_list_view(repo) -> None:
    put_view(repo, "ORDERS_V")
    scope = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, ViewListView)


async def test_the_view_list_view_lists_every_cached_view(repo) -> None:
    put_view(repo, "ORDERS_V", created_on="2026-01-01 00:00:00")
    put_view(repo, "CUSTOMERS_V", created_on="2026-02-02 00:00:00")
    scope = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "CUSTOMERS_V"
        assert table.get_cell_at((1, 0)).plain == "ORDERS_V"
        assert table.get_cell_at((1, 3)).plain == "2026-01-01"


async def test_view_stats_columns_render_when_the_scope_has_data(repo) -> None:
    put_view(repo, "ORDERS_V")
    put_view_stats(
        repo,
        "ORDERS_V",
        LAST_DDL="2026-03-01 08:00:00",
        LAST_ALTERED="2026-03-02 09:30:00",
    )
    scope = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 4)).plain == "2026-03-01"
        assert table.get_cell_at((0, 5)).plain == "2026-03-02 09:30"


async def test_view_stats_columns_are_blank_without_a_successful_stats_pass(
    repo,
) -> None:
    """A missing/failed stats scope must not block the SHOW-sourced columns:
    the row still renders, just with the two SELECT-sourced cells blank."""
    put_view(repo, "ORDERS_V")
    scope = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""


async def test_materialized_view_stats_render_rows_and_size(repo) -> None:
    put_view(repo, "TOTALS_MV", is_materialized="true")
    put_materialized_view_stats(repo, "TOTALS_MV", rows=42, bytes=8192)
    scope = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 1)).plain == "42"
        assert table.get_cell_at((0, 2)).plain == "8.0K"
        assert table.get_cell_at((0, 6)).plain == "Yes"


async def test_a_regular_view_has_blank_rows_and_size(repo) -> None:
    """Not a cache-miss gap: SHOW VIEWS never carries rows/bytes for a
    regular view, so there is nothing to merge in, ever -- unlike Last
    DDL/Last Altered, which are simply absent until a stats pass succeeds."""
    put_view(repo, "ORDERS_V")
    scope = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 1)).plain == ""
        assert table.get_cell_at((0, 2)).plain == ""
        assert table.get_cell_at((0, 6)).plain == ""


def test_view_list_view_scopes_includes_both_stats_scopes(repo) -> None:
    """This is what wires the new scopes into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    view = ViewListView(repo, scope)
    assert view_stats_scope("MYDB", "PUBLIC") in view.scopes()
    assert materialized_view_stats_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_view_row_selects_it(repo) -> None:
    """Reuses the exact path a search hit takes -- see the identical table
    test for the full reasoning."""
    put_view(repo, "ORDERS_V")
    scope = objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected == [ObjectRef(ObjectKind.VIEW, "MYDB", "PUBLIC", "ORDERS_V")]


# ---------------------------------------------------------------- pipe list


async def test_selecting_a_pipes_category_mounts_the_pipe_list_view(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, PipeListView)


async def test_the_status_tab_shows_every_pipes_running_or_stopped_state(
    repo,
) -> None:
    """The whole point of moving Status here from the single-pipe view: one
    table answers "which of my pipes are broken?" for every pipe at once,
    not just whichever one happens to be open."""
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe(repo, "LOAD_CUSTOMERS", invalid_reason="stage dropped")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#pipe-list-status", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "LOAD_CUSTOMERS"
        assert table.get_cell_at((0, 1)).plain == "stopped"
        assert table.get_cell_at((0, 2)).plain == "stage dropped"
        assert table.get_cell_at((1, 0)).plain == "LOAD_ORDERS"
        assert table.get_cell_at((1, 1)).plain == "running"
        assert table.get_cell_at((1, 2)).plain == ""


async def test_the_status_tab_is_active_by_default(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_category(scope)
        await pilot.pause()
        assert view.query_one(TabbedContent).active == "tab-status"


async def test_the_pipe_list_tab_mnemonic_actions_switch_tabs(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_category(scope)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-status"
        view.action_show_tab("history")
        assert tabs.active == "tab-history"
        view.action_show_tab("status")
        assert tabs.active == "tab-status"


async def test_the_pipe_list_view_lists_every_cached_pipe(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe(repo, "LOAD_CUSTOMERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "LOAD_CUSTOMERS"
        assert table.get_cell_at((1, 0)).plain == "LOAD_ORDERS"


async def test_pipe_activity_columns_render_when_the_scope_has_data(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_9.csv.gz",
        FILE_SIZE=2048,
        ROW_COUNT=100,
        STATUS="Loaded",
        LAST_LOAD_TIME="2026-03-01 08:00:00",
        TABLE_NAME="ORDERS",
    )
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 1)).plain == "100"
        assert table.get_cell_at((0, 2)).plain == "2.0K"
        assert table.get_cell_at((0, 3)).plain == "2026-03-01 08:00"
        assert table.get_cell_at((0, 4)).plain == "Loaded"
        assert table.get_cell_at((0, 5)).plain == "ORDERS"
        assert table.get_cell_at((0, 6)).plain == "orders_9.csv.gz"


async def test_pipe_activity_columns_are_blank_without_a_successful_activity_pass(
    repo,
) -> None:
    """A missing/failed activity scope must not block the SHOW-sourced Name
    column: the row still renders, just with the activity cells blank."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 1)).plain == ""
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""
        assert table.get_cell_at((0, 6)).plain == ""


def test_pipe_list_view_scopes_includes_the_activity_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    view = PipeListView(repo, scope)
    assert pipe_activity_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_pipe_row_selects_it(repo) -> None:
    """See test_pressing_enter_on_a_table_row_selects_it -- same handler,
    same reasoning, added to pipes only per the current request."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected == [ObjectRef(ObjectKind.PIPE, "MYDB", "PUBLIC", "LOAD_ORDERS")]


# ---------------------------------------------------------------- task list


async def test_selecting_a_tasks_category_mounts_the_task_list_view(repo) -> None:
    put_task(repo, "REFRESH_ORDERS")
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, TaskListView)


async def test_the_task_list_view_has_no_tabs(repo) -> None:
    """Explicitly not tabbed like PipeListView -- there is only one thing to
    show for a schema's tasks right now."""
    put_task(repo, "REFRESH_ORDERS")
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert len(pane.query(TabbedContent)) == 0


async def test_the_task_list_view_lists_every_cached_task(repo) -> None:
    put_task(repo, "REFRESH_ORDERS", warehouse="MYWH", schedule="5 MINUTE")
    put_task(repo, "REFRESH_TOTALS", warehouse="OTHERWH", schedule="1 HOUR")
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "REFRESH_ORDERS"
        assert table.get_cell_at((0, 1)).plain == "MYWH"
        assert table.get_cell_at((0, 2)).plain == "5 MINUTE"
        assert table.get_cell_at((1, 0)).plain == "REFRESH_TOTALS"


async def test_the_state_column_shows_snowflakes_own_wording_unmodified(repo) -> None:
    """DESIGN.md's "mirror Snowflake, don't reinterpret it" rule -- the raw
    show_tasks.state value is shown verbatim, not relabelled."""
    put_task(repo, "REFRESH_ORDERS", state="suspended")
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 3)).plain == "suspended"


async def test_state_and_last_run_status_stay_visually_distinct(repo) -> None:
    """A suspended-but-last-succeeded task and an enabled-but-last-failed task
    must be distinguishable -- State (show_tasks.state) and Last Run Status
    (the merged activity STATE) are different vocabularies from different
    columns, not the same field shown twice."""
    put_task(repo, "REFRESH_ORDERS", state="suspended")
    put_task_activity(repo, "REFRESH_ORDERS", STATE="SUCCEEDED")
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 3)).plain == "suspended"
        assert table.get_cell_at((0, 5)).plain == "SUCCEEDED"


async def test_task_activity_columns_render_when_the_scope_has_data(repo) -> None:
    put_task(repo, "REFRESH_ORDERS")
    put_task_activity(
        repo,
        "REFRESH_ORDERS",
        SCHEDULED_TIME="2026-03-01 08:00:00",
        STATE="FAILED",
    )
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 4)).plain == "2026-03-01 08:00"
        assert table.get_cell_at((0, 5)).plain == "FAILED"


async def test_task_activity_columns_are_blank_without_a_successful_activity_pass(
    repo,
) -> None:
    """A missing/failed activity scope must not block the SHOW-sourced
    columns: the row still renders, just with the activity cells blank."""
    put_task(repo, "REFRESH_ORDERS")
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""


def test_task_list_view_scopes_includes_the_activity_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    view = TaskListView(repo, scope)
    assert task_activity_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_task_row_selects_it(repo) -> None:
    """See test_pressing_enter_on_a_table_row_selects_it -- same handler,
    same reasoning."""
    put_task(repo, "REFRESH_ORDERS")
    scope = objects_scope(ObjectKind.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected == [
        ObjectRef(ObjectKind.TASK, "MYDB", "PUBLIC", "REFRESH_ORDERS")
    ]


async def test_an_unregistered_category_kind_shows_a_placeholder(repo) -> None:
    """Only Tables, Views, Pipes and Tasks have list views so far; Stages,
    the one category kind still unregistered, falls back to the same
    placeholder convention as an unregistered object kind."""
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        result = pane.show_category(objects_scope(ObjectKind.STAGE, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert result is None
        assert pane.query_one("#detail-placeholder", Static)


async def test_a_category_cache_update_repaints_the_list(repo) -> None:
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert pane.query_one("#detail-table-list", DataTable).row_count == 0

        put_table(repo, "ORDERS")
        pane.cache_updated(scope)
        await pilot.pause()
        assert pane.query_one("#detail-table-list", DataTable).row_count == 1


# --------------------------------------------------------- schema/database


async def test_showing_a_schema_mounts_the_schema_view(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        assert isinstance(pane.view, SchemaView)


async def test_showing_a_database_mounts_the_database_view(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_database("MYDB")
        await pilot.pause()
        assert isinstance(pane.view, DatabaseView)


async def test_moving_between_two_schemas_reuses_the_schema_view(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view_a = pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        view_b = pane.show_schema(SchemaRef(database="MYDB", name="OTHER"))
        await pilot.pause()
        assert view_a is view_b
        assert pane.view.ref.name == "OTHER"


async def test_moving_from_a_schema_to_a_database_replaces_the_view(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        pane.show_database("MYDB")
        await pilot.pause()
        assert isinstance(pane.view, DatabaseView)
        assert len(pane.query(SchemaView)) == 0


async def test_moving_from_a_database_to_a_schema_replaces_the_view(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_database("MYDB")
        await pilot.pause()
        pane.show_schema(SchemaRef(database="MYDB", name="PUBLIC"))
        await pilot.pause()
        assert isinstance(pane.view, SchemaView)
        assert len(pane.query(DatabaseView)) == 0


async def test_missing_grants_and_tags_are_requested(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show_schema(ref)
        await pilot.pause()
    assert grants_scope("MYDB", "PUBLIC").key in app.needed
    assert tag_references_scope("MYDB", "PUBLIC").key in app.needed


async def test_cached_grants_and_tags_are_not_re_requested(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_grant(repo, "MYDB", "PUBLIC")
    put_tag(repo, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show_schema(ref)
        await pilot.pause()
    assert app.needed == []


async def test_the_grants_table_lists_every_cached_grant(repo) -> None:
    """granted_to isn't filtered down to roles -- a DATABASE ROLE grant shows
    up right alongside a ROLE grant."""
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_grant(
        repo,
        "MYDB",
        "PUBLIC",
        granted_to="ROLE",
        grantee_name="ANALYST",
        privilege="USAGE",
    )
    put_grant(
        repo,
        "MYDB",
        "PUBLIC",
        granted_to="DATABASE ROLE",
        grantee_name="APP_ROLE",
        privilege="SELECT",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        table = pane.query_one("#grants-table", DataTable)
        assert table.row_count == 2
        granted_to = {table.get_cell_at((0, 4)).plain, table.get_cell_at((1, 4)).plain}
        assert granted_to == {"ROLE", "DATABASE ROLE"}


async def test_the_tags_table_lists_name_and_value_pairs(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_tag(repo, "MYDB", "PUBLIC", TAG_NAME="COST_CENTER", TAG_VALUE="ENG")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        table = pane.query_one("#tags-table", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 0)).plain == "COST_CENTER"
        assert table.get_cell_at((0, 1)).plain == "ENG"


async def test_tags_render_blank_without_a_successful_tag_references_pass(repo) -> None:
    """The no-warehouse degrade-blank case: Grants (metadata layer) still
    renders even though Tags (a SELECT that needs a warehouse) has nothing
    cached -- same pattern as
    test_table_stats_columns_are_blank_without_a_successful_stats_pass."""
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    put_grant(repo, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        assert pane.query_one("#grants-table", DataTable).row_count == 1
        assert pane.query_one("#tags-table", DataTable).row_count == 0


async def test_a_cache_update_for_the_shown_schemas_grants_repaints(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        assert pane.query_one("#grants-table", DataTable).row_count == 0

        put_grant(repo, "MYDB", "PUBLIC")
        pane.cache_updated(grants_scope("MYDB", "PUBLIC"))
        await pilot.pause()
        assert pane.query_one("#grants-table", DataTable).row_count == 1


async def test_a_cache_update_for_the_shown_schemas_tags_repaints(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_schema(ref)
        await pilot.pause()
        assert pane.query_one("#tags-table", DataTable).row_count == 0

        put_tag(repo, "MYDB", "PUBLIC")
        pane.cache_updated(tag_references_scope("MYDB", "PUBLIC"))
        await pilot.pause()
        assert pane.query_one("#tags-table", DataTable).row_count == 1


def test_schema_view_scopes_includes_both_grants_and_tags(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    view = SchemaView(repo, ref)
    assert grants_scope("MYDB", "PUBLIC") in view.scopes()
    assert tag_references_scope("MYDB", "PUBLIC") in view.scopes()


def test_database_view_scopes_includes_both_grants_and_tags(repo) -> None:
    view = DatabaseView(repo, "MYDB")
    assert grants_scope("MYDB") in view.scopes()
    assert tag_references_scope("MYDB") in view.scopes()


async def test_the_grants_tab_is_active_by_default(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_schema(ref)
        await pilot.pause()
        assert view.query_one(TabbedContent).active == "tab-grants"


async def test_the_schema_tab_mnemonic_actions_switch_tabs(repo) -> None:
    ref = SchemaRef(database="MYDB", name="PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_schema(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-grants"
        view.action_show_tab("tags")
        assert tabs.active == "tab-tags"
        view.action_show_tab("grants")
        assert tabs.active == "tab-grants"


async def test_the_database_tab_mnemonic_actions_switch_tabs(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_database("MYDB")
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-grants"
        view.action_show_tab("tags")
        assert tabs.active == "tab-tags"
