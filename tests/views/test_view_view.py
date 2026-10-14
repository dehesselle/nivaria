"""ViewView: a view's columns, its rows, its grants and its defining SQL."""

from __future__ import annotations

from textual.widgets import DataTable, Static, TabbedContent, TextArea

from snowcone.models import (
    ObjectRef,
    ObjectType,
    data_preview_scope,
    object_grants_scope,
)
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_columns,
    put_data_preview,
    put_object_grant,
    put_view,
)

_GRANTS_KEY = "grants:views:MYDB.PUBLIC.ORDERS_V"
_DATA_KEY = "data_preview:MYDB.PUBLIC.ORDERS_V"


async def test_the_view_columns_tab_is_active_by_default(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.view.query_one(TabbedContent).active == "tab-columns"


async def test_the_view_tabs_are_in_alphabetical_order(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert [pane.id for pane in tabs.query("TabPane")] == [
            "tab-columns",
            "tab-data",
            "tab-grants",
            "tab-sql",
        ]


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
        assert table.get_cell_at((0, 1)).plain == "ID"
        assert table.get_cell_at((1, 2)).plain == "FLOAT"


async def test_the_view_sql_tab_shows_the_views_sql_text(repo) -> None:
    ref = put_view(repo, "ORDERS_V", text="SELECT id, total FROM orders")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        text_area = pane.query_one("#detail-sql", TextArea)
        assert text_area.text == "SELECT id, total FROM orders"


async def test_the_view_sql_tab_falls_back_when_nothing_is_cached(repo) -> None:
    ref = ObjectRef(ObjectType.VIEW, "MYDB", "PUBLIC", "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        text_area = pane.query_one("#detail-sql", TextArea)
        assert text_area.text == "-- not cached"


async def test_the_view_tab_mnemonics_switch_tabs(repo) -> None:
    """`d` is Data on a view as on a table, so the SQL tab is `s`."""
    ref = put_view(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        # Bindings reach the view only through the focus chain.
        pane.query_one("#detail-columns", DataTable).focus()
        await pilot.press("s")
        assert tabs.active == "tab-sql"
        await pilot.press("d")
        assert tabs.active == "tab-data"
        await pilot.press("g")
        assert tabs.active == "tab-grants"
        await pilot.press("c")
        assert tabs.active == "tab-columns"


async def test_a_view_asks_for_no_grants_or_rows_until_their_tab_is_shown(
    repo,
) -> None:
    """Both cost a round trip per view, and a view's rows run its whole
    query -- passing over views in the tree must not pay for either."""
    ref = put_view(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        assert _GRANTS_KEY not in app.needed
        assert _DATA_KEY not in app.needed
        assert object_grants_scope(ref) not in view.scopes()
        assert data_preview_scope("MYDB", "PUBLIC", "ORDERS_V") not in view.scopes()


async def test_showing_the_grants_tab_asks_for_the_views_grants(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.action_show_tab("grants")
        await pilot.pause()
        assert _GRANTS_KEY in app.needed
        assert object_grants_scope(ref) in view.scopes()


async def test_the_view_grants_tab_lists_one_row_per_grantee(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    view_grant = {"object_type": ObjectType.VIEW}
    put_object_grant(repo, "ORDERS_V", grantee_name="ANALYST", **view_grant)
    put_object_grant(
        repo, "ORDERS_V", grantee_name="ETL", privilege="INSERT", **view_grant
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.action_show_tab("grants")
        await pilot.pause()
        table = pane.query_one("#detail-grants", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((1, 0)).plain == "ETL"


async def test_showing_the_data_tab_asks_for_the_views_rows(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.action_show_tab("data")
        await pilot.pause()
        assert _DATA_KEY in app.needed
        assert data_preview_scope("MYDB", "PUBLIC", "ORDERS_V") in view.scopes()


async def test_the_view_data_tab_shows_the_cached_rows(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    put_data_preview(repo, "ORDERS_V", {"ID": 1, "TOTAL": 2.5})
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.action_show_tab("data")
        await pilot.pause()
        table = pane.query_one("#detail-data", DataTable)
        assert [cell.plain for cell in table.get_row_at(0)] == ["1", "2.5"]


async def test_an_empty_view_says_so(repo) -> None:
    ref = put_view(repo, "ORDERS_V")
    put_data_preview(repo, "ORDERS_V")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.action_show_tab("data")
        await pilot.pause()
        note = pane.query_one("#detail-data-note", Static)
        assert note.display
        assert str(note.render()) == "No rows in this view."


async def test_a_capped_view_preview_gives_no_total(repo) -> None:
    """SHOW VIEWS has no row count, so there is no "of n" to give."""
    ref = put_view(repo, "ORDERS_V")
    put_data_preview(repo, "ORDERS_V", {"ID": 1}, {"ID": 2}, truncated=True)
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.action_show_tab("data")
        await pilot.pause()
        note = pane.query_one("#detail-data-note", Static)
        assert str(note.render()) == "Showing 2 rows, in no particular order."
