"""ViewListView: every view in a schema."""

from __future__ import annotations

from textual.widgets import DataTable

from snowcone.models import (
    ObjectRef,
    ObjectType,
    materialized_view_stats_scope,
    objects_scope,
    view_stats_scope,
)
from snowcone.ui.views.view_list_view import ViewListView
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_materialized_view_stats,
    put_view,
    put_view_stats,
)


async def test_selecting_a_views_category_mounts_the_view_list_view(repo) -> None:
    put_view(repo, "ORDERS_V")
    scope = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, ViewListView)


async def test_the_view_list_view_lists_every_cached_view(repo) -> None:
    put_view(repo, "ORDERS_V", created_on="2026-01-01 00:00:00")
    put_view(repo, "CUSTOMERS_V", created_on="2026-02-02 00:00:00")
    scope = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
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
    scope = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.get_cell_at((0, 4)).plain == "2026-03-01"
        assert table.get_cell_at((0, 5)).plain == "2026-03-02 09:30"


async def test_view_stats_columns_are_blank_without_a_successful_stats_pass(
    repo,
) -> None:
    """A missing/failed stats scope must not block the SHOW-sourced columns:
    the row still renders, just with the two SELECT-sourced cells blank."""
    put_view(repo, "ORDERS_V")
    scope = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""


async def test_materialized_view_stats_render_rows_and_size(repo) -> None:
    put_view(repo, "TOTALS_MV", is_materialized="true")
    put_materialized_view_stats(repo, "TOTALS_MV", rows=42, bytes=8192)
    scope = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.get_cell_at((0, 1)).plain == "42"
        assert table.get_cell_at((0, 2)).plain == "8.0K"
        assert table.get_cell_at((0, 6)).plain == "Yes"


async def test_a_regular_view_has_blank_rows_and_size(repo) -> None:
    """Not a cache-miss gap: SHOW VIEWS never carries rows/bytes for a
    regular view, so there is nothing to merge in, ever -- unlike Last
    DDL/Last Altered, which are simply absent until a stats pass succeeds."""
    put_view(repo, "ORDERS_V")
    scope = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.get_cell_at((0, 1)).plain == ""
        assert table.get_cell_at((0, 2)).plain == ""
        assert table.get_cell_at((0, 6)).plain == ""


def test_view_list_view_scopes_includes_both_stats_scopes(repo) -> None:
    """This is what wires the new scopes into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
    view = ViewListView(repo, scope)
    assert view_stats_scope("MYDB", "PUBLIC") in view.scopes()
    assert materialized_view_stats_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_view_row_selects_it(repo) -> None:
    """Reuses the exact path a search hit takes -- see the identical table
    test for the full reasoning."""
    put_view(repo, "ORDERS_V")
    scope = objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
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
    assert app.selected == [ObjectRef(ObjectType.VIEW, "MYDB", "PUBLIC", "ORDERS_V")]
