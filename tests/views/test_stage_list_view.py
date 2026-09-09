"""StageListView: every stage in a schema.

This list is the only place a stage is shown in any detail -- a stage has no
per-object view, because its `SHOW STAGES` row is the whole picture (see
DESIGN.md, "What decides a view's shape").

The hover tests below cover the same shared-`HoverTooltipTable` pattern as
test_table_list_hover.py -- see that file for the offset-fudge reasoning.
"""

from __future__ import annotations

from textual.coordinate import Coordinate
from textual.widgets import DataTable

from snowcone.format import format_timestamp_full
from snowcone.models import ObjectType, objects_scope
from snowcone.ui.widgets.detail_pane import DetailPane
from tests.views.harness import Harness, put_stage


async def _mounted_table(app: Harness, pilot) -> DataTable:
    pane = app.query_one(DetailPane)
    pane.show_category(objects_scope(ObjectType.STAGE, "MYDB", "PUBLIC"))
    await pilot.pause()
    return pane.query_one("#detail-list", DataTable)


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


async def test_the_stage_list_view_lists_every_cached_stage(repo) -> None:
    """Every column comes straight off `show_stages` -- no second scope, and
    no per-object view to fall back on if this one is wrong."""
    put_stage(
        repo,
        "MY_STAGE",
        type="EXTERNAL",
        storage_integration="MY_INT",
        url="s3://bucket/prefix/",
        created_on="2026-01-02 13:45:07",
        comment="landing zone",
    )
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        assert table.row_count == 1
        assert [c.plain for c in table.get_row_at(0)] == [
            "MY_STAGE",
            "EXTERNAL",
            "MY_INT",
            "s3://bucket/prefix/",
            "2026-01-02",
            "landing zone",
        ]


async def test_an_internal_stage_leaves_its_external_only_columns_blank(repo) -> None:
    """Why Type earns a column: Storage Integration and URL are empty for an
    internal stage, and Type is what explains the blank rather than a cache
    miss (the same job Materialized does in the Views list)."""
    # What Snowflake actually returns for an internal stage: no integration,
    # no URL. put_stage's defaults describe an external one.
    put_stage(repo, "INTERNAL_STAGE", type="INTERNAL", storage_integration="", url="")
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        row = [c.plain for c in table.get_row_at(0)]
        assert row[1] == "INTERNAL"
        assert row[2] == ""
        assert row[3] == ""
