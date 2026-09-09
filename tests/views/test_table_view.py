"""TableView: a table's column list."""

from __future__ import annotations

from textual.widgets import DataTable

from snowcone.models import (
    columns_scope,
)
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_columns,
    put_table,
)


def _click_header(table: DataTable, label: str) -> DataTable.HeaderSelected:
    """Build the message a real click on the `label` header posts -- the
    same event `ColumnsView.on_data_table_header_selected` bubbles up."""
    column = next(c for c in table.ordered_columns if c.label.plain == label)
    index = next(i for i, c in enumerate(table.ordered_columns) if c is column)
    return DataTable.HeaderSelected(table, column.key, index, column.label)


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
        assert table.get_cell_at((0, 1)).plain == "ID"
        assert table.get_cell_at((1, 2)).plain == "FLOAT"


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
        assert table.get_cell_at((0, 2)).plain == "NUMBER(38,0)"
        assert table.get_cell_at((1, 2)).plain == "TIMESTAMP_LTZ(9)"


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


async def test_columns_default_to_declaration_order(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["TOTAL", "ID"]


async def test_clicking_the_name_header_sorts_alphabetically_then_reverses(
    repo,
) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)

        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["ID", "TOTAL"]

        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["TOTAL", "ID"]


async def test_clicking_ordinal_after_a_name_sort_returns_declaration_order(
    repo,
) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)

        table.post_message(_click_header(table, "Name"))
        await pilot.pause()
        table.post_message(_click_header(table, "Ordinal"))
        await pilot.pause()
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == ["TOTAL", "ID"]


async def test_moving_to_another_table_resets_the_sort(repo) -> None:
    """Re-pointing the reused view at a different object must not carry the
    previous table's sort choice along with it."""
    orders = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("TOTAL", "FLOAT"), ("ID", "NUMBER"))
    customers = put_table(repo, "CUSTOMERS")
    put_columns(repo, "CUSTOMERS", ("NAME", "TEXT"), ("EMAIL", "TEXT"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(orders)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        table.post_message(_click_header(table, "Name"))
        await pilot.pause()

        pane.show(customers)
        await pilot.pause()
        table = app.query_one("#detail-columns", DataTable)
        assert [table.get_cell_at((r, 1)).plain for r in range(2)] == [
            "NAME",
            "EMAIL",
        ]
