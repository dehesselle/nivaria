"""Hovering "Created" tooltips the full timestamp behind a truncated cell.

Ported from test_table_list_hover.py, which covers the same DataTable-
subclass tooltip pattern this view reuses (see stage_list_view.py's
_StageListDataTable) -- see that file for the offset-fudge reasoning.
"""

from __future__ import annotations

import pytest
from test_detail_pane import Harness, put_stage
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.format import format_timestamp_full
from snowcone.models import ObjectKind, objects_scope
from snowcone.ui.widgets.detail_pane import DetailPane


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


async def _mounted_table(app: Harness, pilot) -> DataTable:
    pane = app.query_one(DetailPane)
    pane.show_category(objects_scope(ObjectKind.STAGE, "MYDB", "PUBLIC"))
    await pilot.pause()
    return pane.query_one("#detail-table-list", DataTable)


async def _hover_cell(pilot, table: DataTable, row: int, column: int) -> None:
    cell = table._get_cell_region(Coordinate(row, column))
    await pilot.hover(table, offset=(cell.x + 1, cell.y))


async def test_hovering_created_tooltips_the_full_timestamp(repo) -> None:
    raw = "2026-01-02 13:45:07"
    put_stage(repo, "MY_STAGE", created_on=raw)
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        await _hover_cell(pilot, table, 0, 4)
        assert table.tooltip == format_timestamp_full(raw)


async def test_hovering_a_non_tooltip_column_shows_no_tooltip(repo) -> None:
    put_stage(repo, "MY_STAGE")
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        await _hover_cell(pilot, table, 0, 0)
        assert table.tooltip is None
