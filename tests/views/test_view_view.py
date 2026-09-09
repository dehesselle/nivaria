"""ViewView: a view's columns and its defining SQL."""

from __future__ import annotations

from textual.widgets import DataTable, TabbedContent, TextArea

from snowcone.models import (
    ObjectRef,
    ObjectType,
)
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_columns,
    put_view,
)


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
        text_area = pane.query_one("#detail-definition", TextArea)
        assert text_area.text == "SELECT id, total FROM orders"


async def test_the_view_definition_tab_falls_back_when_nothing_is_cached(repo) -> None:
    ref = ObjectRef(ObjectType.VIEW, "MYDB", "PUBLIC", "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        text_area = pane.query_one("#detail-definition", TextArea)
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
