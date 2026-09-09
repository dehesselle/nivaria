"""Hovering "Loaded" tooltips the full timestamp behind a truncated cell.

Same DataTable-subclass tooltip pattern as test_table_list_hover.py (see
views/tables.py's HoverTooltipTable) -- one tooltip-bearing column here
instead of three.
"""

from __future__ import annotations

from textual.coordinate import Coordinate
from textual.widgets import DataTable

from snowcone.format import format_timestamp_full
from snowcone.models import ObjectType, objects_scope
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import Harness, put_pipe, put_pipe_activity


async def _mounted_table(app: Harness, pilot) -> DataTable:
    pane = app.query_one(DetailPane)
    view = pane.show_category(objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC"))
    await pilot.pause()
    # The table lives on the "Load history" tab, not the default "Status"
    # one -- switch there first, same as a user pressing "l".
    view.action_show_tab("history")
    await pilot.pause()
    return pane.query_one("#detail-list", DataTable)


async def _hover_cell(pilot, table: DataTable, row: int, column: int) -> None:
    cell = table._get_cell_region(Coordinate(row, column))
    await pilot.hover(table, offset=(cell.x + 1, cell.y))


async def test_hovering_loaded_tooltips_the_full_timestamp(repo) -> None:
    raw = "2026-03-01 08:15:30"
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS", LAST_LOAD_TIME=raw)
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        await _hover_cell(pilot, table, 0, 3)
        assert table.tooltip == format_timestamp_full(raw)


async def test_hovering_a_non_timestamp_column_sets_no_tooltip(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        await _hover_cell(pilot, table, 0, 0)  # Name
        assert table.tooltip is None


async def test_hovering_a_row_without_an_activity_pass_sets_no_tooltip(repo) -> None:
    """A blank cell (no successful activity pass) must not tooltip a stale or
    missing value -- format_timestamp_full("") is falsy, so the handler
    treats it the same as no tooltip at all."""
    put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        await _hover_cell(pilot, table, 0, 3)  # Loaded, never fetched
        assert table.tooltip is None


async def test_hovering_past_the_last_row_clears_the_tooltip(repo) -> None:
    """DataTable leaves hover_coordinate untouched over filler space with no
    row/column meta, so a tooltip set while over a real cell would otherwise
    never get the reactive change needed to clear it -- see
    HoverTooltipTable._on_mouse_move."""
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS", LAST_LOAD_TIME="2026-01-02 13:45:07")
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        # DataTable auto-sizes to its content by default, leaving no filler
        # space below one row -- force room for the mouse to move into.
        table.styles.height = 20
        await pilot.pause()

        await _hover_cell(pilot, table, 0, 3)
        assert table.tooltip is not None

        below_rows = table._get_row_region(table.row_count - 1)
        await pilot.hover(
            table, offset=(below_rows.x + 1, below_rows.y + below_rows.height + 1)
        )
        assert table.tooltip is None
