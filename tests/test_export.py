# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The `, e` chords: which table they export, under what name, holding what.

`DataTable.to_csv`, `App.deliver_text` and `App.copy_to_clipboard` are tcoda's
and Textual's. What is nivaria's is choosing the table, naming the file
honestly, keeping the raw value behind every shortened cell, writing it as Jira
markup, and saying how it went.
"""

from __future__ import annotations

import asyncio
import codecs
import csv
import io
import json
import locale
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tcoda.chords import ChordMixin
from tcoda.settings import config
from textual.app import App
from textual.coordinate import Coordinate
from textual.widgets import DataTable, TabbedContent

import nivaria.export as export_module
import nivaria.ui.app as app_module
from nivaria.cache.db import Database
from nivaria.cache.repository import Repository
from nivaria.export import (
    EXPORT_DELIVERY,
    csv_delimiter,
    export_filename,
    jira_table,
)
from nivaria.format import format_timestamp_full
from nivaria.models import (
    ObjectRef,
    ObjectType,
    SchemaRef,
    column_tag_references_scope,
    objects_scope,
    pipe_status_scope,
    table_stats_scope,
    tag_references_scope,
)
from nivaria.settings import CSV_DELIMITER
from nivaria.ui.app import NivariaApp
from nivaria.ui.screens.main import MainScreen
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.fakes import FakeConnector, make_pool
from tests.test_main_screen import RecordingFetch, settle
from tests.views.harness import (
    load,
    put_columns,
    put_data_preview,
    put_grant,
    put_materialized_view_stats,
    put_object_grant,
    put_pipe,
    put_pipe_activity,
    put_pipe_activity_history,
    put_pipe_history,
    put_pipe_status,
    put_stage,
    put_stage_stats,
    put_table,
    put_table_stats,
    put_tag,
    put_task,
    put_task_activity,
    put_task_activity_history,
    put_task_history,
    put_view,
    put_view_stats,
)

#: Inside every history display window, which is what decides whether a row
#: shows at all.
RECENT = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
RECENTER = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()


class ExportApp(ChordMixin, App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self._repo = repo
        self._pool, _ = make_pool(size=1)
        self._pool.connect_all()
        self.delivered = asyncio.Event()

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self._repo, RecordingFetch(), self._pool))

    def on_delivery_complete(self) -> None:
        self.delivered.set()

    @property
    def pane(self) -> DetailPane:
        return self.screen.query_one(DetailPane)


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


@pytest.fixture
def downloads(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where `deliver_text` saves, with the delimiter pinned: its default
    follows this machine's locale, which no test may depend on."""
    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr("textual.app.user_downloads_path", lambda: folder)
    config.set(CSV_DELIMITER, "comma")
    return folder


async def press_export(app: ExportApp, pilot) -> None:
    await pilot.press("comma", "e", "c")
    await asyncio.wait_for(app.delivered.wait(), timeout=5)


def saved(downloads: Path) -> Path:
    [path] = list(downloads.iterdir())
    return path


def stem(path: Path) -> str:
    """A saved file's name without its timestamp and extension."""
    name, count = re.subn(r"_\d{8}-\d{6}\.csv$", "", path.name)
    assert count == 1, path.name
    return name


def parsed(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.reader(file))


def column(rows: list[list[str]], header: str) -> list[str]:
    index = rows[0].index(header)
    return [row[index] for row in rows[1:]]


async def show_tab(app: ExportApp, pilot, tab: str) -> None:
    app.pane.view.query_one(TabbedContent).active = tab
    await settle(pilot)


async def hover_cell(pilot, table: DataTable, row: int, header: str) -> None:
    index = next(
        number
        for number, candidate in enumerate(table.ordered_columns)
        if candidate.key.value == header
    )
    cell = table._get_cell_region(Coordinate(row, index))
    await pilot.hover(table, offset=(cell.x + 1, cell.y))
    await pilot.pause()


# ------------------------------------------------------------- every table


_TABLES = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
_ORDERS = ObjectRef(ObjectType.TABLE, "MYDB", "PUBLIC", "ORDERS")
_REPORT = ObjectRef(ObjectType.VIEW, "MYDB", "PUBLIC", "REPORT")
_LOAD_ORDERS = ObjectRef(ObjectType.PIPE, "MYDB", "PUBLIC", "LOAD_ORDERS")
_NIGHTLY = ObjectRef(ObjectType.TASK, "MYDB", "PUBLIC", "NIGHTLY")
_PUBLIC = SchemaRef(database="MYDB", name="PUBLIC")

_COLUMN_HEADER = ["Ordinal", "Name", "Type", "Nullable", "Default", "Tags", "Comment"]
_GRANT_HEADER = ["Grantee", "Granted To", "Privileges"]
_TAG_HEADER = ["Tag", "Value"]


def _seed_tables(repo: Repository) -> None:
    put_table(repo, "ORDERS")
    put_table_stats(repo, "ORDERS")


def _seed_views(repo: Repository) -> None:
    put_view(repo, "REPORT")
    put_view_stats(repo, "REPORT")
    put_materialized_view_stats(repo, "REPORT")


def _seed_stages(repo: Repository) -> None:
    put_stage(repo, "RAW")
    put_stage_stats(repo, "RAW")


def _seed_tasks(repo: Repository) -> None:
    put_task(repo, "NIGHTLY")
    put_task_activity(repo, "NIGHTLY")


def _seed_pipes(repo: Repository) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_status(repo, {"LOAD_ORDERS": "RUNNING"})


def _seed_columns(repo: Repository, name: str = "ORDERS") -> None:
    put_columns(repo, name, ("ID", "NUMBER"))
    load(repo, column_tag_references_scope("MYDB", "PUBLIC", name), [])


def _seed_view_columns(repo: Repository) -> None:
    put_view(repo, "REPORT")
    _seed_columns(repo, "REPORT")


def _seed_pipe_history(repo: Repository) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=RECENT)


def _seed_task_history(repo: Repository) -> None:
    put_task(repo, "NIGHTLY")
    put_task_activity_history(repo, "NIGHTLY", SCHEDULED_TIME=RECENT)


@dataclass(frozen=True)
class Case:
    seed: Callable[[Repository], None]
    show: Callable[[DetailPane], object]
    name: str
    header: list[str]
    tab: str | None = None


_CASES = {
    "tables": Case(
        _seed_tables,
        lambda pane: pane.show_category(_TABLES),
        "tables_MYDB.PUBLIC",
        ["Name", "Rows", "Size", "Created", "Last DDL", "Last Altered", "Comment"],
    ),
    "views": Case(
        _seed_views,
        lambda pane: pane.show_category(
            objects_scope(ObjectType.VIEW, "MYDB", "PUBLIC")
        ),
        "views_MYDB.PUBLIC",
        [
            "Name",
            "Rows",
            "Size",
            "Created",
            "Last DDL",
            "Last Altered",
            "Materialized",
            "Comment",
        ],
    ),
    "stages": Case(
        _seed_stages,
        lambda pane: pane.show_category(
            objects_scope(ObjectType.STAGE, "MYDB", "PUBLIC")
        ),
        "stages_MYDB.PUBLIC",
        [
            "Name",
            "Type",
            "Storage Integration",
            "URL",
            "Created",
            "Last Altered",
            "Comment",
        ],
    ),
    "tasks": Case(
        _seed_tasks,
        lambda pane: pane.show_category(
            objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
        ),
        "tasks_MYDB.PUBLIC",
        [
            "Name",
            "Warehouse",
            "Schedule",
            "State",
            "Last Scheduled",
            "Last Run State",
            "Comment",
        ],
    ),
    "pipes status": Case(
        _seed_pipes,
        lambda pane: pane.show_category(
            objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
        ),
        "pipes_status_MYDB.PUBLIC",
        ["Name", "Created", "Execution State", "Pending Files", "Invalid Reason"],
    ),
    "pipes activity": Case(
        _seed_pipes,
        lambda pane: pane.show_category(
            objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
        ),
        "pipes_activity_MYDB.PUBLIC",
        ["Name", "Rows", "Size", "Loaded", "Status", "Target Table", "File Name"],
        tab="tab-activity",
    ),
    "table columns": Case(
        _seed_columns,
        lambda pane: pane.show(_ORDERS),
        "table_columns_MYDB.PUBLIC.ORDERS",
        _COLUMN_HEADER,
    ),
    "view columns": Case(
        _seed_view_columns,
        lambda pane: pane.show(_REPORT),
        "view_columns_MYDB.PUBLIC.REPORT",
        _COLUMN_HEADER,
    ),
    "table grants": Case(
        lambda repo: put_object_grant(repo, "ORDERS"),
        lambda pane: pane.show(_ORDERS),
        "table_grants_MYDB.PUBLIC.ORDERS",
        _GRANT_HEADER,
        tab="tab-grants",
    ),
    "schema grants": Case(
        lambda repo: put_grant(repo, "MYDB", "PUBLIC"),
        lambda pane: pane.show_schema(_PUBLIC),
        "schema_grants_MYDB.PUBLIC",
        _GRANT_HEADER,
    ),
    "schema tags": Case(
        lambda repo: put_tag(repo, "MYDB", "PUBLIC"),
        lambda pane: pane.show_schema(_PUBLIC),
        "schema_tags_MYDB.PUBLIC",
        _TAG_HEADER,
        tab="tab-tags",
    ),
    "database grants": Case(
        lambda repo: put_grant(repo, "MYDB", None),
        lambda pane: pane.show_database("MYDB"),
        "database_grants_MYDB",
        _GRANT_HEADER,
    ),
    "database tags": Case(
        lambda repo: put_tag(repo, "MYDB", None),
        lambda pane: pane.show_database("MYDB"),
        "database_tags_MYDB",
        _TAG_HEADER,
        tab="tab-tags",
    ),
    # A history is never known to be whole, even with no cap hit: it only ever
    # covers a time window.
    "pipe history": Case(
        _seed_pipe_history,
        lambda pane: pane.show(_LOAD_ORDERS),
        "partial_pipe_history_MYDB.PUBLIC.LOAD_ORDERS",
        ["Rows", "Size", "Loaded", "Status", "File Name"],
    ),
    "task history": Case(
        _seed_task_history,
        lambda pane: pane.show(_NIGHTLY),
        "partial_task_history_MYDB.PUBLIC.NIGHTLY",
        ["Scheduled", "Started", "Completed", "State"],
    ),
}


@pytest.mark.parametrize("case", _CASES.values(), ids=_CASES.keys())
async def test_each_table_exports_under_a_name_saying_what_it_is(
    repo, downloads, case: Case
) -> None:
    """Also what holds every table to tcoda's `DataTable`: Textual's has no
    `to_csv`, so one built from it fails here."""
    case.seed(repo)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        case.show(app.pane)
        await settle(pilot)
        if case.tab is not None:
            await show_tab(app, pilot, case.tab)
        await press_export(app, pilot)

    path = saved(downloads)
    rows = parsed(path)
    assert stem(path) == case.name
    assert rows[0] == case.header
    assert len(rows) > 1


#: What a Jira copy's first line says about each case's table, before escaping.
_TITLES = {
    "tables": "tables in MYDB.PUBLIC",
    "views": "views in MYDB.PUBLIC",
    "stages": "stages in MYDB.PUBLIC",
    "tasks": "tasks in MYDB.PUBLIC",
    "pipes status": "pipes status in MYDB.PUBLIC",
    "pipes activity": "pipes activity in MYDB.PUBLIC",
    "table columns": "table columns for MYDB.PUBLIC.ORDERS",
    "view columns": "view columns for MYDB.PUBLIC.REPORT",
    "table grants": "table grants for MYDB.PUBLIC.ORDERS",
    "schema grants": "schema grants for MYDB.PUBLIC",
    "schema tags": "schema tags for MYDB.PUBLIC",
    "database grants": "database grants for MYDB",
    "database tags": "database tags for MYDB",
    "pipe history": "pipe history for MYDB.PUBLIC.LOAD_ORDERS",
    "task history": "task history for MYDB.PUBLIC.NIGHTLY",
}


def test_every_case_has_a_title() -> None:
    assert _TITLES.keys() == _CASES.keys()


@pytest.mark.parametrize(("case_id", "title"), _TITLES.items(), ids=_TITLES.keys())
async def test_each_jira_copy_says_what_it_is(repo, case_id: str, title: str) -> None:
    case = _CASES[case_id]
    case.seed(repo)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        case.show(app.pane)
        await settle(pilot)
        if case.tab is not None:
            await show_tab(app, pilot, case.tab)
        await pilot.press("comma", "e", "j")
        await settle(pilot)
        assert app.clipboard.splitlines()[0] == export_module._jira_text(title)


async def test_the_table_on_screen_exports_with_the_focus_on_the_tree(
    repo, downloads
) -> None:
    _seed_tables(repo)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        app.screen.tree.focus()
        await pilot.pause()
        await press_export(app, pilot)
    assert stem(saved(downloads)) == "tables_MYDB.PUBLIC"


async def test_the_file_has_a_bom_and_one_line_per_row(repo, downloads) -> None:
    put_table(repo, "ORDERS")
    put_table(repo, "PARTS")
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        await press_export(app, pilot)
    data = saved(downloads).read_bytes()
    assert data.startswith(codecs.BOM_UTF8)
    assert b"\r\r\n" not in data
    assert len(data.splitlines()) == 3


async def test_the_delimiter_setting_reaches_the_file(repo, downloads) -> None:
    config.set(CSV_DELIMITER, "semicolon")
    put_table(repo, "ORDERS")
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        await press_export(app, pilot)
    header = saved(downloads).read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("Name;Rows;Size;")


# ---------------------------------------------------------- partial_ or not


async def _export_tables(repo: Repository, downloads: Path) -> str:
    put_table(repo, "ORDERS")
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        await press_export(app, pilot)
    return stem(saved(downloads))


async def test_a_list_whose_sources_all_finished_loses_the_prefix(
    repo, downloads
) -> None:
    put_table_stats(repo, "ORDERS")
    assert await _export_tables(repo, downloads) == "tables_MYDB.PUBLIC"


async def test_a_list_whose_second_source_failed_stays_partial(repo, downloads) -> None:
    """Its rows are all there, and two of its columns are blank."""
    scope = table_stats_scope("MYDB", "PUBLIC")
    repo.fail_pass(scope, repo.begin_pass(scope), "no warehouse")
    assert await _export_tables(repo, downloads) == "partial_tables_MYDB.PUBLIC"


async def test_a_list_whose_second_source_is_still_running_stays_partial(
    repo, downloads
) -> None:
    repo.begin_pass(table_stats_scope("MYDB", "PUBLIC"))
    assert await _export_tables(repo, downloads) == "partial_tables_MYDB.PUBLIC"


async def test_a_list_with_a_capped_source_stays_partial(repo, downloads) -> None:
    load(repo, table_stats_scope("MYDB", "PUBLIC"), [], truncated=True)
    assert await _export_tables(repo, downloads) == "partial_tables_MYDB.PUBLIC"


async def _copy_tables(repo: Repository) -> str:
    put_table(repo, "ORDERS")
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        await pilot.press("comma", "e", "j")
        await settle(pilot)
        return app.clipboard


async def test_a_complete_jira_copy_is_a_title_and_the_table(repo) -> None:
    put_table_stats(repo, "ORDERS")
    title, blank, header, *_ = (await _copy_tables(repo)).splitlines()
    assert title == "tables in MYDB.PUBLIC"
    assert blank == ""
    assert header.startswith("||Name||Rows||")


async def test_a_partial_jira_copy_says_so_in_italics_under_the_title(repo) -> None:
    load(repo, table_stats_scope("MYDB", "PUBLIC"), [], truncated=True)
    title, note, blank, header, *_ = (await _copy_tables(repo)).splitlines()
    assert title == "tables in MYDB.PUBLIC"
    assert note == "_Partial: may not hold every row Snowflake has._"
    assert blank == ""
    assert header.startswith("||Name||")


async def test_tags_failing_says_nothing_about_a_tables_grants(repo, downloads) -> None:
    """Without a warehouse the tags query fails; SHOW GRANTS needs none."""
    put_object_grant(repo, "ORDERS")
    scope = column_tag_references_scope("MYDB", "PUBLIC", "ORDERS")
    repo.fail_pass(scope, repo.begin_pass(scope), "no warehouse")
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show(_ORDERS)
        await settle(pilot)
        await show_tab(app, pilot, "tab-grants")
        await press_export(app, pilot)
    assert stem(saved(downloads)) == "table_grants_MYDB.PUBLIC.ORDERS"


async def test_failing_tags_leave_the_columns_partial(repo, downloads) -> None:
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    scope = column_tag_references_scope("MYDB", "PUBLIC", "ORDERS")
    repo.fail_pass(scope, repo.begin_pass(scope), "no warehouse")
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show(_ORDERS)
        await settle(pilot)
        await press_export(app, pilot)
    assert stem(saved(downloads)) == "partial_table_columns_MYDB.PUBLIC.ORDERS"


async def test_a_tags_tab_still_being_fetched_stays_partial(repo, downloads) -> None:
    put_tag(repo, "MYDB", "PUBLIC")
    repo.begin_pass(tag_references_scope("MYDB", "PUBLIC"))
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_schema(_PUBLIC)
        await settle(pilot)
        await show_tab(app, pilot, "tab-tags")
        await press_export(app, pilot)
    assert stem(saved(downloads)) == "partial_schema_tags_MYDB.PUBLIC"


# -------------------------------------------------------------- raw values


async def test_a_list_exports_exact_rows_and_size_and_full_timestamps(
    repo, downloads
) -> None:
    raw = "2026-03-01 08:15:30"
    put_table(repo, "ORDERS", rows=1234567, bytes=5000000, created_on=raw)
    put_table_stats(repo, "ORDERS", LAST_DDL=raw, LAST_ALTERED=raw)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        await press_export(app, pilot)

    rows = parsed(saved(downloads))
    assert column(rows, "Rows") == ["1234567"]
    assert column(rows, "Size") == ["5000000"]
    for header in ("Created", "Last DDL", "Last Altered"):
        assert column(rows, header) == [format_timestamp_full(raw)]


async def test_a_jira_copy_holds_the_csvs_raw_values(repo) -> None:
    raw = "2026-03-01 08:15:30"
    put_table(repo, "ORDERS", rows=1234567, bytes=5000000, created_on=raw)
    put_table_stats(repo, "ORDERS", LAST_DDL=raw, LAST_ALTERED=raw)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        await pilot.press("comma", "e", "j")
        await settle(pilot)
        table = app.pane.view.shown_table()
        csv_rows = list(csv.reader(io.StringIO(table.to_csv(tooltips=True))))
        assert app.clipboard == jira_table(
            csv_rows, title="tables in MYDB.PUBLIC", complete=True
        )
        assert "|1234567|5000000|" in app.clipboard
        assert ("information", "Copied 1 row as Jira wiki markup") in toasts(app)


async def test_a_value_the_source_does_not_have_exports_blank(repo, downloads) -> None:
    put_table(repo, "ORDERS", rows=None, bytes=None)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        await press_export(app, pilot)

    rows = parsed(saved(downloads))
    for header in ("Rows", "Size", "Created", "Last DDL", "Last Altered"):
        assert column(rows, header) == [""]


async def test_a_size_of_zero_exports_as_zero_and_not_blank(repo, downloads) -> None:
    put_table(repo, "ORDERS", rows=0, bytes=0)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        await press_export(app, pilot)

    rows = parsed(saved(downloads))
    assert column(rows, "Rows") == ["0"]
    assert column(rows, "Size") == ["0"]


async def test_the_pipes_status_tab_exports_the_exact_pending_files(
    repo, downloads
) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    load(
        repo,
        pipe_status_scope("MYDB", "PUBLIC"),
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "LOAD_ORDERS",
                "PIPE_STATUS": json.dumps({"pendingFileCount": 12345}),
            }
        ],
    )
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC"))
        await settle(pilot)
        await press_export(app, pilot)
    assert column(parsed(saved(downloads)), "Pending Files") == ["12345"]


async def test_a_pipes_history_exports_exact_rows_and_size_and_a_full_timestamp(
    repo, downloads
) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        LAST_LOAD_TIME=RECENT,
        ROW_COUNT=1234567,
        FILE_SIZE=5000000,
    )
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show(_LOAD_ORDERS)
        await settle(pilot)
        await press_export(app, pilot)

    rows = parsed(saved(downloads))
    assert column(rows, "Rows") == ["1234567"]
    assert column(rows, "Size") == ["5000000"]
    assert column(rows, "Loaded") == [format_timestamp_full(RECENT)]


async def test_a_tasks_history_exports_all_three_timestamps_in_full(
    repo, downloads
) -> None:
    put_task(repo, "NIGHTLY")
    put_task_history(
        repo,
        "NIGHTLY",
        SCHEDULED_TIME=RECENT,
        QUERY_START_TIME=RECENTER,
        COMPLETED_TIME=None,
    )
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show(_NIGHTLY)
        await settle(pilot)
        await press_export(app, pilot)

    rows = parsed(saved(downloads))
    assert column(rows, "Scheduled") == [format_timestamp_full(RECENT)]
    assert column(rows, "Started") == [format_timestamp_full(RECENTER)]
    assert column(rows, "Completed") == [""]


async def test_hovering_a_size_tooltips_the_exact_bytes(repo) -> None:
    put_table(repo, "ORDERS", bytes=5000000)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(_TABLES)
        await settle(pilot)
        table = app.pane.query_one("#detail-list", DataTable)
        await hover_cell(pilot, table, 0, "Size")
        assert table.tooltip == "5000000"


async def test_hovering_a_history_timestamp_tooltips_it_in_full(repo) -> None:
    put_task(repo, "NIGHTLY")
    put_task_history(repo, "NIGHTLY", SCHEDULED_TIME=RECENT)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show(_NIGHTLY)
        await settle(pilot)
        table = app.pane.query_one("#detail-history", DataTable)
        await hover_cell(pilot, table, 0, "Scheduled")
        assert table.tooltip == format_timestamp_full(RECENT)


async def test_a_loads_size_on_a_pipes_history_tooltips_the_exact_bytes(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=RECENT, FILE_SIZE=5000000)
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show(_LOAD_ORDERS)
        await settle(pilot)
        table = app.pane.query_one("#detail-history", DataTable)
        await hover_cell(pilot, table, 0, "Size")
        assert table.tooltip == "5000000"


# ---------------------------------------------------------------- refusals


def toasts(app: App[None]) -> list[tuple[str, str]]:
    return [(toast.severity, toast.message) for toast in app._notifications]


async def _refuses(app: ExportApp, pilot, downloads: Path, message: str) -> None:
    """Both chords: no file, nothing copied, and each says why."""
    for chord in ("c", "j"):
        await pilot.press("comma", "e", chord)
        await settle(pilot)
    assert list(downloads.iterdir()) == []
    assert app.clipboard == ""
    assert toasts(app).count(("warning", message)) == 2


_DATA_REFUSAL = "The Data tab's rows are not exported: they can be personal data."
_NO_ROWS = "No rows here to export."


async def test_the_data_tab_is_never_exported(repo, downloads) -> None:
    """docs/DESIGN.md "Table data never reaches the disk"."""
    put_table(repo, "ORDERS")
    put_data_preview(repo, "ORDERS", {"ID": 1})
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show(_ORDERS)
        await settle(pilot)
        await show_tab(app, pilot, "tab-data")
        table = app.pane.query_one("#detail-data", DataTable)
        assert table.row_count == 1
        await _refuses(app, pilot, downloads, _DATA_REFUSAL)


async def test_a_sql_tab_has_nothing_to_export(repo, downloads) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show(_LOAD_ORDERS)
        await settle(pilot)
        await show_tab(app, pilot, "tab-sql")
        await _refuses(app, pilot, downloads, _NO_ROWS)


async def test_an_empty_table_writes_no_file(repo, downloads) -> None:
    load(repo, objects_scope(ObjectType.STAGE, "MYDB", "PUBLIC"), [])
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.show_category(objects_scope(ObjectType.STAGE, "MYDB", "PUBLIC"))
        await settle(pilot)
        await _refuses(app, pilot, downloads, _NO_ROWS)


async def test_nothing_selected_has_nothing_to_export(repo, downloads) -> None:
    app = ExportApp(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.pane.view is None
        await _refuses(app, pilot, downloads, _NO_ROWS)


# ------------------------------------------------------------------ toasts


async def _wait_for_toast(app: App[None], pilot) -> None:
    async with asyncio.timeout(5):
        while not toasts(app):
            await pilot.pause(0.01)


async def test_a_saved_export_says_where_it_went(tmp_path) -> None:
    """With a `[` in the path, which as markup would raise or be eaten."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.deliver_text(
            io.StringIO("a\n"),
            save_directory=tmp_path,
            save_filename="[x].csv",
            name=EXPORT_DELIVERY,
        )
        await _wait_for_toast(app, pilot)
        assert toasts(app) == [("information", f"Saved {tmp_path / '[x].csv'}")]


async def test_a_failed_export_says_so(tmp_path) -> None:
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.deliver_text(
            io.StringIO("a\n"),
            save_directory=tmp_path / "missing",
            save_filename="x.csv",
            name=EXPORT_DELIVERY,
        )
        await _wait_for_toast(app, pilot)
        [(severity, message)] = toasts(app)
    assert severity == "error"
    assert message.startswith("Could not save: ")


async def test_another_delivery_raises_no_export_toast(tmp_path) -> None:
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.deliver_text(
            io.StringIO("a\n"),
            save_directory=tmp_path,
            save_filename="x.csv",
            name="something else",
        )
        async with asyncio.timeout(5):
            while not (tmp_path / "x.csv").exists():
                await pilot.pause(0.01)
        await settle(pilot)
        assert toasts(app) == []


# ---------------------------------------------------------------- filename


NOON = datetime(2026, 11, 7, 12, 30, 5).astimezone()


def test_a_file_known_to_be_complete_has_no_prefix() -> None:
    assert (
        export_filename("tables_DB.S", complete=True, now=NOON)
        == "tables_DB.S_20261107-123005.csv"
    )


def test_anything_else_is_named_partial() -> None:
    assert (
        export_filename("pipe_history_DB.S.P", complete=False, now=NOON)
        == "partial_pipe_history_DB.S.P_20261107-123005.csv"
    )


def test_a_quoted_identifier_cannot_reach_outside_the_filename() -> None:
    name = export_filename('table_columns_DB.S."../a b:c\\d"', complete=True, now=NOON)
    assert name == "table_columns_DB.S._.._a_b_c_d__20261107-123005.csv"


def test_a_name_in_another_alphabet_stays_readable() -> None:
    name = export_filename("tables_DB.ÄRGER", complete=True, now=NOON)
    assert name == "tables_DB.ÄRGER_20261107-123005.csv"


# --------------------------------------------------------------- delimiter


@pytest.mark.parametrize(("choice", "delimiter"), [("comma", ","), ("semicolon", ";")])
def test_a_literal_choice_is_that_character(choice: str, delimiter: str) -> None:
    config.set(CSV_DELIMITER, choice)
    assert csv_delimiter() == delimiter


def test_the_default_asks_the_operating_system(monkeypatch) -> None:
    monkeypatch.setattr(export_module, "_system_list_separator", lambda: "|")
    assert csv_delimiter() == "|"


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows registry")
@pytest.mark.parametrize(
    ("value", "delimiter"),
    [
        (";", ";"),
        (",", ","),
        ("\t", "\t"),
        # Windows allows up to three characters, and `csv` takes one.
        ("||", ","),
        ("", ","),
    ],
)
def test_windows_uses_its_list_separator_if_csv_can(
    monkeypatch, value: str, delimiter: str
) -> None:
    monkeypatch.setattr(
        export_module.winreg, "QueryValueEx", lambda _key, _name: (value, 1)
    )
    assert export_module._windows_list_separator() == delimiter


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows registry")
def test_an_unreadable_windows_setting_falls_back_to_a_comma(monkeypatch) -> None:
    def fail(_key, _name):
        raise OSError

    monkeypatch.setattr(export_module.winreg, "QueryValueEx", fail)
    assert export_module._windows_list_separator() == ","


@pytest.mark.parametrize(("point", "delimiter"), [(",", ";"), (".", ",")])
def test_elsewhere_a_decimal_comma_makes_it_a_semicolon(
    monkeypatch, point: str, delimiter: str
) -> None:
    monkeypatch.setattr(locale, "setlocale", lambda _category, _value=None: "C")
    monkeypatch.setattr(locale, "localeconv", lambda: {"decimal_point": point})
    assert export_module._locale_list_separator() == delimiter


def test_an_unusable_locale_falls_back_to_a_comma(monkeypatch) -> None:
    def setlocale(_category, value=None):
        if value == "":
            raise locale.Error
        return "C"

    monkeypatch.setattr(locale, "setlocale", setlocale)
    assert export_module._locale_list_separator() == ","


def test_looking_up_the_locale_leaves_the_process_as_it_was() -> None:
    before = locale.setlocale(locale.LC_NUMERIC)
    export_module._locale_list_separator()
    assert locale.setlocale(locale.LC_NUMERIC) == before


# ------------------------------------------------------------- Jira markup


def _jira(rows: list[list[str]], *, title: str = "t", complete: bool = True) -> str:
    return jira_table(rows, title=title, complete=complete)


def test_the_header_row_uses_double_bars_and_the_rest_single() -> None:
    assert _jira([["A", "B"], ["1", "2"], ["3", "4"]]) == (
        "t\n\n||A||B||\n|1|2|\n|3|4|"
    )


def test_a_partial_table_has_an_italic_note_under_its_title() -> None:
    assert _jira([["A"], ["1"]], complete=False) == (
        "t\n_Partial: may not hold every row Snowflake has._\n\n||A||\n|1|"
    )


def test_the_title_is_escaped_like_a_cell() -> None:
    assert _jira([["A"], ["1"]], title="pipe history for DB.S.MY_PIPE").startswith(
        "pipe history for DB.S.MY\\_PIPE\n"
    )


def test_a_bar_cannot_end_a_cell() -> None:
    assert _jira([["A"], ["x|y"]]).endswith(r"|x\|y|")


@pytest.mark.parametrize("char", list("{}[]*_+-^~?!#"))
def test_markup_characters_are_escaped(char: str) -> None:
    assert _jira([["A"], [f"a{char}b"]]).endswith(rf"|a\{char}b|")


def test_a_backslash_neither_escapes_nor_breaks_a_line() -> None:
    assert _jira([["A"], [r"C:\|x"]]).endswith(r"|C:&#92;\|x|")


@pytest.mark.parametrize("value", ["one\ntwo", "one\r\ntwo", "one\rtwo"])
def test_a_multi_line_value_stays_in_its_cell(value: str) -> None:
    assert _jira([["A"], [value]]).endswith(r"|one\\ two|")


@pytest.mark.parametrize("value", ["", "  "])
def test_an_empty_cell_does_not_become_a_header_separator(value: str) -> None:
    assert _jira([["", "A"], [value, "1"]]).endswith("|| ||A||\n| |1|")
