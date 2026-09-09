"""PipeListView: every pipe in a schema, status and load history."""

from __future__ import annotations

from textual.widgets import DataTable, TabbedContent

from snowcone.models import (
    ObjectRef,
    ObjectType,
    objects_scope,
    pipe_activity_scope,
)
from snowcone.ui.views.pipe_list_view import PipeListView
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_pipe,
    put_pipe_activity,
)


async def test_selecting_a_pipes_category_mounts_the_pipe_list_view(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
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
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-status", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "LOAD_CUSTOMERS"
        assert table.get_cell_at((0, 1)).plain == "STOPPED"
        assert table.get_cell_at((0, 2)).plain == "stage dropped"
        assert table.get_cell_at((1, 0)).plain == "LOAD_ORDERS"
        assert table.get_cell_at((1, 1)).plain == "RUNNING"
        assert table.get_cell_at((1, 2)).plain == ""


async def test_the_status_tab_is_active_by_default(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_category(scope)
        await pilot.pause()
        assert view.query_one(TabbedContent).active == "tab-status"


async def test_the_pipe_list_tab_mnemonic_actions_switch_tabs(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
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
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
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
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
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
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 1)).plain == ""
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""
        assert table.get_cell_at((0, 6)).plain == ""


def test_pipe_list_view_scopes_includes_the_activity_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    view = PipeListView(repo, scope)
    assert pipe_activity_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_pipe_row_selects_it(repo) -> None:
    """See test_pressing_enter_on_a_table_row_selects_it -- same handler,
    same reasoning, added to pipes only per the current request."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected == [ObjectRef(ObjectType.PIPE, "MYDB", "PUBLIC", "LOAD_ORDERS")]
