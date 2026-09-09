"""TableListView: every table in a schema."""

from __future__ import annotations

from textual.widgets import DataTable

from snowcone.models import (
    ObjectRef,
    ObjectType,
    objects_scope,
    table_stats_scope,
)
from snowcone.ui.views.table_list_view import TableListView
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    load,
    put_table,
    put_table_stats,
)


async def test_selecting_a_tables_category_mounts_the_table_list_view(repo) -> None:
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, TableListView)


async def test_the_table_list_view_lists_every_cached_table(repo) -> None:
    put_table(repo, "ORDERS", rows=42, bytes=8192, created_on="2026-01-01 00:00:00")
    put_table(repo, "CUSTOMERS", rows=7, bytes=1024, created_on="2026-02-02 00:00:00")
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
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
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.get_cell_at((0, 3)).plain == "2026-01-01"
        assert table.get_cell_at((0, 4)).plain == "2026-03-01"
        assert table.get_cell_at((0, 5)).plain == "2026-03-02 09:30"


async def test_table_stats_columns_are_blank_without_a_successful_stats_pass(
    repo,
) -> None:
    """A missing/failed stats scope must not block the SHOW-sourced columns:
    the row still renders, just with the two SELECT-sourced cells blank."""
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""


def test_table_list_view_scopes_includes_the_stats_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    view = TableListView(repo, scope)
    assert table_stats_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_table_row_selects_it(repo) -> None:
    """Reuses the exact path a search hit takes (ObjectSelected ->
    MainScreen._select) -- this only has to prove the row posts the message
    with the right ref; the reveal/show wiring itself is search's own,
    already-tested plumbing."""
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
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
    assert app.selected == [ObjectRef(ObjectType.TABLE, "MYDB", "PUBLIC", "ORDERS")]


async def test_moving_between_two_schemas_tables_reuses_the_list_view(repo) -> None:
    put_table(repo, "ORDERS")
    other_scope = objects_scope(ObjectType.TABLE, "MYDB", "OTHER")
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
        view_a = pane.show_category(objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"))
        await pilot.pause()
        view_b = pane.show_category(other_scope)
        await pilot.pause()
        assert view_a is view_b
        assert pane.query_one("#detail-list", DataTable).row_count == 1
