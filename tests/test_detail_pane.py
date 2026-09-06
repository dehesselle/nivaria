"""The detail pane and its view registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    columns_scope,
    objects_scope,
)
from snowcone.ui.messages import ScopeNeeded
from snowcone.ui.views.base import registered_kinds, view_for
from snowcone.ui.views.pipe_view import PipeView
from snowcone.ui.views.table_list_view import TableListView
from snowcone.ui.views.table_view import TableView, ViewView
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

    def compose(self) -> ComposeResult:
        yield DetailPane(self._repo, id="detail")

    def on_scope_needed(self, event: ScopeNeeded) -> None:
        self.needed.append(event.scope.key)


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


def put_columns(repo: Repository, table: str, *columns: tuple[str, str]) -> None:
    load(
        repo,
        columns_scope("MYDB", "PUBLIC", table),
        [
            {
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
            for position, (name, data_type) in enumerate(columns, start=1)
        ],
    )


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
    load(repo, objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC"), [row])
    return ObjectRef(ObjectKind.PIPE, "MYDB", "PUBLIC", name)


# ---------------------------------------------------------------- registry


def test_every_kind_the_tree_shows_has_a_view_or_is_known_missing() -> None:
    """A kind with no view is a placeholder, not a crash -- but the ones that
    do have views must actually be registered by importing their modules."""
    assert view_for(ObjectKind.TABLE) is TableView
    assert view_for(ObjectKind.VIEW) is ViewView
    assert view_for(ObjectKind.PIPE) is PipeView


def test_registration_is_by_object_kind() -> None:
    assert {ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.PIPE} <= registered_kinds()


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


async def test_the_table_view_summarises_rows_and_size(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        summary = str(pane.query_one("#detail-summary", Static).render())
    # The name lives in the context bar above; repeating it here would spend a
    # line of a small pane saying nothing new.
    assert "MYDB.PUBLIC.ORDERS" not in summary
    assert "1,234 rows" in summary
    assert "4.0K" in summary


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


# --------------------------------------------------------------- pipe view


async def test_a_pipe_shows_its_definition_not_columns(repo) -> None:
    """A different shape entirely -- which is what the registry is for."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert "COPY INTO" in str(pane.query_one("#pipe-definition", Static).render())
        assert len(pane.query("#detail-columns")) == 0


async def test_a_healthy_pipe_hides_the_problem_banner(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-problem", Static).display is False


async def test_a_stopped_pipe_says_so_loudly(repo) -> None:
    """A stopped pipe looks exactly like a healthy one until you read
    invalid_reason, which is why it gets a banner of its own."""
    ref = put_pipe(repo, "LOAD_ORDERS", invalid_reason="stage dropped")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        banner = pane.query_one("#pipe-problem", Static)
        assert banner.display is True
        assert "stage dropped" in str(banner.render())


async def test_a_pipe_needs_no_extra_fetch(repo) -> None:
    """Everything a pipe shows came with the SHOW PIPES row already cached."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert app.needed == []


async def test_each_view_supplies_its_own_context(repo) -> None:
    """A view owns its detail body and its context bar together; splitting
    them across two registries would let them drift apart."""
    table = put_table(repo, "ORDERS")
    pipe = put_pipe(repo, "LOAD_ORDERS", invalid_reason="stage dropped")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        table_context = [w.id for w in pane.show(table).compose_context()]
        await pilot.pause()
        pipe_view = pane.show(pipe)
        await pilot.pause()
        assert "context-title" in table_context
        assert "stopped" in pipe_view._status()


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
        assert table.get_cell_at((1, 3)).plain == "2026-01-01 00:00:00"


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


async def test_an_unregistered_category_kind_shows_a_placeholder(repo) -> None:
    """Only Tables has a list view so far; the other category kinds fall back
    to the same placeholder convention as an unregistered object kind."""
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        result = pane.show_category(objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC"))
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
