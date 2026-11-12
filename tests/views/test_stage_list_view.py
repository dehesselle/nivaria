# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""StageListView: every stage in a schema.

This list is the only place a stage is shown in any detail -- a stage has no
per-object view (see docs/DESIGN.md, "What decides a view's shape"). Every
column but Last Altered comes from `SHOW STAGES`; Last Altered comes from the
stage stats scope.

The hover tests below cover the same shared-`HoverTooltipTable` pattern as
test_table_list_hover.py -- see that file for the offset-fudge reasoning.
"""

from __future__ import annotations

from textual.coordinate import Coordinate
from textual.widgets import DataTable

from nivaria.format import format_timestamp_full
from nivaria.models import ObjectType, objects_scope, stage_stats_scope
from nivaria.ui.views.stage_list_view import StageListView
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import Harness, put_stage, put_stage_stats


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


async def test_hovering_last_altered_tooltips_the_full_timestamp(repo) -> None:
    raw = "2026-03-02 09:30:07"
    put_stage(repo, "MY_STAGE")
    put_stage_stats(repo, "MY_STAGE", LAST_ALTERED=raw)
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        await _hover_cell(pilot, table, 0, 5)
        assert table.tooltip == format_timestamp_full(raw)


async def test_hovering_a_non_tooltip_column_shows_no_tooltip(repo) -> None:
    put_stage(repo, "MY_STAGE")
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        await _hover_cell(pilot, table, 0, 0)
        assert table.tooltip is None


async def test_the_stage_list_view_lists_every_cached_stage(repo) -> None:
    """Every column but Last Altered comes straight off `show_stages`, and
    there is no per-object view to fall back on if this one is wrong."""
    put_stage(
        repo,
        "MY_STAGE",
        type="EXTERNAL",
        storage_integration="MY_INT",
        url="s3://bucket/prefix/",
        created_on="2026-01-02 13:45:07",
        comment="landing zone",
    )
    put_stage_stats(repo, "MY_STAGE", LAST_ALTERED="2026-03-02 09:30:00")
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
            "2026-03-02 09:30",
            "landing zone",
        ]


async def test_last_altered_is_blank_without_a_successful_stats_pass(repo) -> None:
    """A missing or failed stats scope must not block the SHOW-sourced
    columns: the row still renders, with only Last Altered blank."""
    put_stage(repo, "MY_STAGE", created_on="2026-01-02 13:45:07")
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        table = await _mounted_table(app, pilot)
        assert table.row_count == 1
        assert table.get_cell_at((0, 4)).plain == "2026-01-02"
        assert table.get_cell_at((0, 5)).plain == ""


def test_the_stage_list_view_scopes_include_the_stats_scope(repo) -> None:
    """What puts the stats scope into the staleness sweep, which reads the
    detail view's scopes()."""
    view = StageListView(repo, objects_scope(ObjectType.STAGE, "MYDB", "PUBLIC"))
    assert stage_stats_scope("MYDB", "PUBLIC") in view.scopes()


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
