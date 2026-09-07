"""The detail pane and its view registry."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static, TabbedContent, TextArea

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    columns_scope,
    objects_scope,
    pipe_activity_scope,
    pipe_history_scope,
    table_stats_scope,
)
from snowcone.settings import PIPE_HISTORY_SHOW_DAYS, config
from snowcone.ui.messages import ObjectSelected, ScopeNeeded
from snowcone.ui.views.base import registered_kinds, view_for
from snowcone.ui.views.pipe_list_view import PipeListView
from snowcone.ui.views.pipe_view import PipeView
from snowcone.ui.views.table_list_view import TableListView
from snowcone.ui.views.table_view import TableView, ViewView
from snowcone.ui.widgets.detail_pane import DetailPane


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self._repo = repo
        self.needed: list[str] = []
        self.selected: list[ObjectRef] = []

    def compose(self) -> ComposeResult:
        yield DetailPane(self._repo, id="detail")

    def on_scope_needed(self, event: ScopeNeeded) -> None:
        self.needed.append(event.scope.key)

    def on_object_selected(self, event: ObjectSelected) -> None:
        self.selected.append(event.ref)


def load(repo: Repository, scope, rows) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


def put_table(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "kind": "TABLE",
        "comment": "",
        "rows": 1234,
        "bytes": 4096,
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectKind.TABLE, "MYDB", "PUBLIC", name)


def put_columns(repo: Repository, table: str, *columns: tuple) -> None:
    """Each column is (name, data_type) or (name, data_type, extra), where
    extra is a dict of additional INFORMATION_SCHEMA fields, e.g. precision."""
    rows = []
    for position, column in enumerate(columns, start=1):
        name, data_type, *rest = column
        row = {
            "TABLE_CATALOG": "MYDB",
            "TABLE_SCHEMA": "PUBLIC",
            "TABLE_NAME": table,
            "COLUMN_NAME": name,
            "ORDINAL_POSITION": position,
            "DATA_TYPE": data_type,
            "IS_NULLABLE": "YES",
            "COLUMN_DEFAULT": None,
            "COMMENT": "",
        }
        if rest:
            row.update(rest[0])
        rows.append(row)
    load(repo, columns_scope("MYDB", "PUBLIC", table), rows)


def put_table_stats(repo: Repository, table: str, **extra) -> None:
    row = {
        "TABLE_CATALOG": "MYDB",
        "TABLE_SCHEMA": "PUBLIC",
        "TABLE_NAME": table,
        "LAST_DDL": "2026-01-01 00:00:00",
        "LAST_ALTERED": "2026-01-02 00:00:00",
    }
    row.update(extra)
    load(repo, table_stats_scope("MYDB", "PUBLIC"), [row])


def put_pipe(repo: Repository, name: str, **extra) -> ObjectRef:
    row = {
        "database_name": "MYDB",
        "schema_name": "PUBLIC",
        "name": name,
        "definition": "COPY INTO t FROM @s",
        "owner": "LOADER",
        "notification_channel": "arn:sqs:...",
        "comment": "",
    }
    row.update(extra)
    existing = repo.raw_rows(objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC"))
    load(
        repo,
        objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC"),
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )
    return ObjectRef(ObjectKind.PIPE, "MYDB", "PUBLIC", name)


def put_pipe_activity(repo: Repository, pipe: str, **extra) -> None:
    row = {
        "PIPE_CATALOG_NAME": "MYDB",
        "PIPE_SCHEMA_NAME": "PUBLIC",
        "PIPE_NAME": pipe,
        "FILE_NAME": "orders_1.csv.gz",
        "FILE_SIZE": 1024,
        "ROW_COUNT": 42,
        "STATUS": "Loaded",
        "LAST_LOAD_TIME": "2026-01-02 03:04:05",
        "TABLE_CATALOG_NAME": "MYDB",
        "TABLE_SCHEMA_NAME": "PUBLIC",
        "TABLE_NAME": "ORDERS",
    }
    row.update(extra)
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


def put_pipe_history(repo: Repository, pipe: str, **extra) -> None:
    row = {
        "FILE_NAME": "orders_9.csv.gz",
        "FILE_SIZE": 2048,
        "ROW_COUNT": 100,
        "STATUS": "Loaded",
        "LAST_LOAD_TIME": "2026-03-01 08:00:00",
        "PIPE_NAME": pipe,
    }
    row.update(extra)
    scope = pipe_history_scope("MYDB", "PUBLIC", pipe)
    existing = repo.raw_rows(scope)
    load(
        repo,
        scope,
        [
            *[{k: v for k, v in e.items() if not k.startswith("_")} for e in existing],
            row,
        ],
    )


# ---------------------------------------------------------------- registry


def test_every_kind_the_tree_shows_has_a_view_or_is_known_missing() -> None:
    """A kind with no view is a placeholder, not a crash -- but the ones that
    do have views must actually be registered by importing their modules."""
    assert view_for(ObjectKind.TABLE) is TableView
    assert view_for(ObjectKind.VIEW) is ViewView
    assert view_for(ObjectKind.PIPE) is PipeView


def test_registration_is_by_object_kind() -> None:
    assert {ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.PIPE} <= registered_kinds()


def test_registering_a_duplicate_kind_is_refused() -> None:
    """Two views for one kind means one silently wins; better to fail loudly
    at import time than to wonder which is showing."""
    from snowcone.ui.views.base import ObjectView, register_view

    with pytest.raises(ValueError):

        @register_view
        class Duplicate(ObjectView):
            OBJECT_KIND = ObjectKind.TABLE

            def update_content(self) -> None:
                pass


# ------------------------------------------------------------------ swapping


async def test_showing_a_table_mounts_the_table_view(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"), ("TOTAL", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert isinstance(pane.view, TableView)


async def test_moving_between_two_tables_reuses_the_view(repo) -> None:
    """The reason the pane swaps on kind rather than on object: arrowing down
    three hundred tables must not remount a widget three hundred times."""
    first = put_table(repo, "ORDERS")
    second = put_table(repo, "CUSTOMERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view_a = pane.show(first)
        await pilot.pause()
        view_b = pane.show(second)
        await pilot.pause()
        assert view_a is view_b
        assert pane.view.ref.name == "CUSTOMERS"


async def test_moving_to_a_different_kind_replaces_the_view(repo) -> None:
    table = put_table(repo, "ORDERS")
    pipe = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(table)
        await pilot.pause()
        pane.show(pipe)
        await pilot.pause()
        assert isinstance(pane.view, PipeView)
        assert len(pane.query(TableView)) == 0


async def test_an_unregistered_kind_shows_a_placeholder(repo) -> None:
    """A kind the tree can produce but nothing has a view for yet must not
    take the app down."""
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        result = pane.show(ObjectRef(ObjectKind.STAGE, "MYDB", "PUBLIC", "S1"))
        await pilot.pause()
        assert result is None
        assert pane.query_one("#detail-placeholder", Static)


async def test_clearing_empties_the_pane(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        pane.clear()
        await pilot.pause()
        assert pane.view is None
        assert pane.scopes() == []


# -------------------------------------------------------------- table view


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
        assert table.get_cell_at((0, 0)).plain == "ID"
        assert table.get_cell_at((1, 1)).plain == "FLOAT"


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
        assert table.get_cell_at((0, 1)).plain == "NUMBER(38,0)"
        assert table.get_cell_at((1, 1)).plain == "TIMESTAMP_LTZ(9)"


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


# --------------------------------------------------------------- pipe view


async def test_a_pipe_shows_load_history_not_columns(repo) -> None:
    """A different shape entirely -- which is what the registry is for."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable)
        assert len(pane.query("#detail-columns")) == 0


async def test_a_stopped_pipes_status_says_so(repo) -> None:
    """A stopped pipe looks exactly like a healthy one until you read
    invalid_reason -- shown in the Status tab as its own column, not buried
    in a sentence."""
    ref = put_pipe(repo, "LOAD_ORDERS", invalid_reason="stage dropped")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-status", DataTable)
        assert table.get_cell_at((0, 0)).plain == "LOAD_ORDERS"
        assert table.get_cell_at((0, 1)).plain == "stopped"
        assert table.get_cell_at((0, 2)).plain == "stage dropped"


async def test_a_running_pipes_status_says_so(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-status", DataTable)
        assert table.get_cell_at((0, 0)).plain == "LOAD_ORDERS"
        assert table.get_cell_at((0, 1)).plain == "running"
        assert table.get_cell_at((0, 2)).plain == ""


async def test_an_uncached_pipes_status_says_so(repo) -> None:
    """A pipe that has never been fetched (only a scope reference exists)
    shows "not cached" rather than a blank or crashing lookup."""
    ref = ObjectRef(ObjectKind.PIPE, "MYDB", "PUBLIC", "GHOST")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-status", DataTable)
        assert table.get_cell_at((0, 0)).plain == "GHOST"
        assert table.get_cell_at((0, 1)).plain == "not cached"


async def test_opening_a_pipe_requests_its_activity_scope(repo) -> None:
    """The base facts came with the SHOW PIPES row already cached, but load
    history is backed by its own SELECT scope -- see pipe_view.py -- and is
    requested the moment the view opens, same as a table's columns scope."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert pipe_activity_scope("MYDB", "PUBLIC").key in app.needed


async def test_a_pipe_with_no_activity_data_never_requests_history_or_errors(
    repo,
) -> None:
    """A pipe that has never loaded anything has no cached activity data, so
    its target table cannot be resolved (Snowflake exposes it nowhere else --
    see Repository.pipe_target_table). That must not repeatedly fail a fetch
    it can never succeed at: history_scope is simply never requested until a
    target becomes resolvable, and the view shows an empty table instead of
    an error."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable).row_count == 0
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key not in app.needed


async def test_a_pipe_with_activity_data_requests_its_history_scope_too(repo) -> None:
    """Once account_usage_copy_history has a row for this pipe, its target
    table is resolvable and history_scope becomes worth asking for."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key in app.needed


async def test_a_pipes_history_table_is_empty_with_no_activity_or_history_cached(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable).row_count == 0


def _days_ago(days: float) -> str:
    """A relative, always-recent-enough timestamp -- these tests filter by
    the show window against the real current time, so a fixed calendar date
    would eventually age out of it."""
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat()


async def test_a_pipes_history_table_lists_rows_from_both_sources(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=_days_ago(60),
        ROW_COUNT=42,
    )
    put_pipe_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_9.csv.gz",
        LAST_LOAD_TIME=_days_ago(5),
        ROW_COUNT=100,
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-history", DataTable)
        assert table.row_count == 2
        # Sorted newest first.
        assert table.get_cell_at((0, 4)).plain == "orders_9.csv.gz"
        assert table.get_cell_at((1, 4)).plain == "orders_1.csv.gz"


async def test_a_pipes_history_ignores_another_pipes_activity_rows(repo) -> None:
    """account_usage_copy_history holds every pipe's rows for the schema
    together -- a single pipe's view must filter to its own name."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_OTHER", FILE_NAME="other.csv.gz")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable).row_count == 0


async def test_the_near_real_time_source_wins_on_an_overlapping_row(repo) -> None:
    """Merged by (FILE_NAME, LAST_LOAD_TIME) -- the near-real-time source is
    inserted last, so an identical key from the backfill is overlaid rather
    than duplicated."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    when = _days_ago(10)
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=when,
        STATUS="Load in progress",
    )
    put_pipe_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=when,
        STATUS="Loaded",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-history", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 3)).plain == "Loaded"


async def test_rows_older_than_the_show_window_are_hidden_but_stay_cached(repo) -> None:
    """The display window can be shorter than the fetch window on purpose --
    shrinking it is a display-only change, since the wider range is still in
    the cache either way (see settings.PIPE_HISTORY_SHOW_DAYS)."""
    config.set(PIPE_HISTORY_SHOW_DAYS, 30)
    ref = put_pipe(repo, "LOAD_ORDERS")
    old = datetime.now().astimezone() - timedelta(days=31)
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="ancient.csv.gz",
        LAST_LOAD_TIME=old.isoformat(),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history", DataTable).row_count == 0
    # Still in the cache -- only hidden from this paint.
    assert repo.raw_rows(pipe_activity_scope("MYDB", "PUBLIC"))


async def test_the_highlighted_rows_error_shows_in_the_history_detail_panel(
    repo,
) -> None:
    """Too long for a table column -- shown below the table instead, for
    whichever row the cursor is currently on."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        LAST_LOAD_TIME=_days_ago(1),
        FIRST_ERROR_MESSAGE="Numeric value 'x' is not recognized",
        FIRST_ERROR_LINE_NUMBER=42,
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#pipe-history-detail", Static)
        assert panel.display
        assert "Line 42" in str(panel.render())
        assert "Numeric value 'x' is not recognized" in str(panel.render())


async def test_a_row_with_no_error_leaves_the_history_detail_panel_hidden(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS", LAST_LOAD_TIME=_days_ago(1))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#pipe-history-detail", Static)
        assert not panel.display


async def test_highlighting_a_different_history_row_updates_the_detail_panel(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="bad.csv.gz",
        LAST_LOAD_TIME=_days_ago(1),
        FIRST_ERROR_MESSAGE="boom",
        FIRST_ERROR_LINE_NUMBER=1,
    )
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="good.csv.gz",
        LAST_LOAD_TIME=_days_ago(2),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#pipe-history", DataTable)
        panel = pane.query_one("#pipe-history-detail", Static)
        # Newest first: row 0 is "bad.csv.gz", the errored one.
        assert panel.display
        table.move_cursor(row=1)
        await pilot.pause()
        assert not panel.display
        table.move_cursor(row=0)
        await pilot.pause()
        assert panel.display


async def test_a_repaint_does_not_leave_a_stale_error_showing(repo) -> None:
    """A cache update while a row is highlighted must resync against
    whatever is actually on that row now, not trust the last message."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    when = _days_ago(1)
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        LAST_LOAD_TIME=when,
        FIRST_ERROR_MESSAGE="boom",
        FIRST_ERROR_LINE_NUMBER=1,
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-history-detail", Static).display

        # Same file/time key, now with no error -- overwrites the row.
        put_pipe_activity(repo, "LOAD_ORDERS", LAST_LOAD_TIME=when)
        pane.cache_updated(pipe_activity_scope("MYDB", "PUBLIC"))
        await pilot.pause()
        assert not pane.query_one("#pipe-history-detail", Static).display


async def test_the_definition_tab_shows_the_pipes_raw_sql(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS", definition="COPY INTO t FROM @s")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert (
            pane.query_one("#pipe-definition", TextArea).text == "COPY INTO t FROM @s"
        )


async def test_the_definition_tab_falls_back_when_uncached(repo) -> None:
    ref = ObjectRef(ObjectKind.PIPE, "MYDB", "PUBLIC", "GHOST")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#pipe-definition", TextArea).text == "-- not cached"


async def test_the_tab_mnemonic_actions_switch_tabs(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-status"
        view.action_show_tab("history")
        assert tabs.active == "tab-history"
        view.action_show_tab("definition")
        assert tabs.active == "tab-definition"
        view.action_show_tab("status")
        assert tabs.active == "tab-status"


def test_pipe_view_scopes_always_includes_activity(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    view = PipeView(repo, ref)
    assert pipe_activity_scope("MYDB", "PUBLIC") in view.scopes()


def test_pipe_view_scopes_excludes_history_until_a_target_is_resolvable(repo) -> None:
    """This is what keeps the background sweep (MainScreen._sweep, which
    watches self.detail.scopes()) from repeatedly retrying a fetch that has
    no target table to give -- not just the interactive ensure() path."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    view = PipeView(repo, ref)
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS") not in view.scopes()

    put_pipe_activity(repo, "LOAD_ORDERS")
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS") in view.scopes()


# --------------------------------------------------------------- table list


async def test_selecting_a_tables_category_mounts_the_table_list_view(repo) -> None:
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, TableListView)


async def test_the_table_list_view_lists_every_cached_table(repo) -> None:
    put_table(repo, "ORDERS", rows=42, bytes=8192, created_on="2026-01-01 00:00:00")
    put_table(repo, "CUSTOMERS", rows=7, bytes=1024, created_on="2026-02-02 00:00:00")
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
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
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 3)).plain == "2026-01-01"
        assert table.get_cell_at((0, 4)).plain == "2026-03-01"
        assert table.get_cell_at((0, 5)).plain == "2026-03-02 09:30"


async def test_table_stats_columns_are_blank_without_a_successful_stats_pass(
    repo,
) -> None:
    """A missing/failed stats scope must not block the SHOW-sourced columns:
    the row still renders, just with the two SELECT-sourced cells blank."""
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""


def test_table_list_view_scopes_includes_the_stats_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    view = TableListView(repo, scope)
    assert table_stats_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_table_row_selects_it(repo) -> None:
    """Reuses the exact path a search hit takes (ObjectSelected ->
    MainScreen._select) -- this only has to prove the row posts the message
    with the right ref; the reveal/show wiring itself is search's own,
    already-tested plumbing."""
    put_table(repo, "ORDERS")
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected == [ObjectRef(ObjectKind.TABLE, "MYDB", "PUBLIC", "ORDERS")]


async def test_moving_between_two_schemas_tables_reuses_the_list_view(repo) -> None:
    put_table(repo, "ORDERS")
    other_scope = objects_scope(ObjectKind.TABLE, "MYDB", "OTHER")
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
        view_a = pane.show_category(objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC"))
        await pilot.pause()
        view_b = pane.show_category(other_scope)
        await pilot.pause()
        assert view_a is view_b
        assert pane.query_one("#detail-table-list", DataTable).row_count == 1


# ---------------------------------------------------------------- pipe list


async def test_selecting_a_pipes_category_mounts_the_pipe_list_view(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, PipeListView)


async def test_the_pipe_list_view_lists_every_cached_pipe(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe(repo, "LOAD_CUSTOMERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "LOAD_CUSTOMERS"
        assert table.get_cell_at((1, 0)).plain == "LOAD_ORDERS"


async def test_pipe_activity_columns_render_when_the_scope_has_data(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_9.csv.gz",
        FILE_SIZE=2048,
        ROW_COUNT=100,
        STATUS="Loaded",
        LAST_LOAD_TIME="2026-03-01 08:00:00",
        TABLE_NAME="ORDERS",
    )
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.get_cell_at((0, 1)).plain == "100"
        assert table.get_cell_at((0, 2)).plain == "2.0K"
        assert table.get_cell_at((0, 3)).plain == "2026-03-01 08:00"
        assert table.get_cell_at((0, 4)).plain == "Loaded"
        assert table.get_cell_at((0, 5)).plain == "ORDERS"
        assert table.get_cell_at((0, 6)).plain == "orders_9.csv.gz"


async def test_pipe_activity_columns_are_blank_without_a_successful_activity_pass(
    repo,
) -> None:
    """A missing/failed activity scope must not block the SHOW-sourced Name
    column: the row still renders, just with the activity cells blank."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 1)).plain == ""
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""
        assert table.get_cell_at((0, 6)).plain == ""


def test_pipe_list_view_scopes_includes_the_activity_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    view = PipeListView(repo, scope)
    assert pipe_activity_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_pipe_row_selects_it(repo) -> None:
    """See test_pressing_enter_on_a_table_row_selects_it -- same handler,
    same reasoning, added to pipes only per the current request."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-table-list", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected == [ObjectRef(ObjectKind.PIPE, "MYDB", "PUBLIC", "LOAD_ORDERS")]


async def test_an_unregistered_category_kind_shows_a_placeholder(repo) -> None:
    """Only Tables and Pipes have list views so far; the other category kinds
    fall back to the same placeholder convention as an unregistered object
    kind."""
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        result = pane.show_category(objects_scope(ObjectKind.VIEW, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert result is None
        assert pane.query_one("#detail-placeholder", Static)


async def test_a_category_cache_update_repaints_the_list(repo) -> None:
    scope = objects_scope(ObjectKind.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert pane.query_one("#detail-table-list", DataTable).row_count == 0

        put_table(repo, "ORDERS")
        pane.cache_updated(scope)
        await pilot.pause()
        assert pane.query_one("#detail-table-list", DataTable).row_count == 1
