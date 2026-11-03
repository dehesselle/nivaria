# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""PipeListView: every pipe in a schema, status and load history."""

from __future__ import annotations

from textual.widgets import DataTable, TabbedContent

from nivaria.format import format_date, format_timestamp_minutes
from nivaria.models import (
    ObjectRef,
    ObjectType,
    objects_scope,
    pipe_activity_scope,
    pipe_status_scope,
)
from nivaria.ui.views.pipe_list_view import PipeListView
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    load,
    put_pipe,
    put_pipe_activity,
    put_pipe_status,
)


async def test_selecting_a_pipes_category_mounts_the_pipe_list_view(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, PipeListView)


async def test_the_status_tab_shows_every_pipes_execution_state_from_snowflake(
    repo,
) -> None:
    """The whole point of moving Status here from the single-pipe view: one
    table answers "which of my pipes are broken?" for every pipe at once,
    not just whichever one happens to be open. A paused pipe has no
    invalid_reason, so only SYSTEM$PIPE_STATUS can say it is paused."""
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe(repo, "LOAD_CUSTOMERS", invalid_reason="stage dropped")
    put_pipe_status(
        repo, {"LOAD_CUSTOMERS": "STOPPED_STAGE_DROPPED", "LOAD_ORDERS": "PAUSED"}
    )
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-status", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "LOAD_CUSTOMERS"
        assert table.get_cell_at((0, 2)).plain == "STOPPED_STAGE_DROPPED"
        assert table.get_cell_at((0, 5)).plain == "stage dropped"
        assert table.get_cell_at((1, 0)).plain == "LOAD_ORDERS"
        assert table.get_cell_at((1, 2)).plain == "PAUSED"
        assert table.get_cell_at((1, 5)).plain == ""


async def test_a_pipe_whose_status_was_never_read_shows_no_execution_state(
    repo,
) -> None:
    """An empty invalid_reason is not evidence of RUNNING: a paused pipe has
    none either. Until Snowflake has been asked, the cell says nothing."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-status", DataTable)
        assert table.get_cell_at((0, 2)).plain == ""


async def test_the_status_tab_shows_when_each_pipe_was_created(repo) -> None:
    raw = "2026-01-02 13:45:07"
    put_pipe(repo, "LOAD_ORDERS", created_on=raw)
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-status", DataTable)
        assert table.get_cell_at((0, 1)).plain == format_date(raw)


async def test_the_status_scope_is_watched_only_while_the_status_tab_is_shown(
    repo,
) -> None:
    """Each pass costs Snowflake time per pipe, so it is neither refreshed
    nor routed to a tab nobody is looking at."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    status = pipe_status_scope("MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_category(scope)
        await pilot.pause()
        assert status in view.scopes()
        assert status.key in app.needed
        view.action_show_tab("activity")
        await pilot.pause()
        assert status not in view.scopes()
        app.needed.clear()
        view.action_show_tab("status")
        await pilot.pause()
        assert status in view.scopes()
        assert status.key in app.needed


async def test_the_status_scope_waits_for_the_pipe_list_it_names_pipes_from(
    repo,
) -> None:
    """A pass before SHOW PIPES has answered would name no pipes, complete
    empty, and then read as fresh for a whole cache age."""
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    status = pipe_status_scope("MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_category(scope)
        await pilot.pause()
        assert status not in view.scopes()
        assert status.key not in app.needed


async def test_the_status_tab_is_active_by_default(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_category(scope)
        await pilot.pause()
        assert view.query_one(TabbedContent).active == "tab-status"


async def test_the_pipe_list_tab_mnemonic_actions_switch_tabs(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show_category(scope)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-status"
        view.action_show_tab("activity")
        assert tabs.active == "tab-activity"
        view.action_show_tab("status")
        assert tabs.active == "tab-status"


async def test_the_pipe_list_view_lists_every_cached_pipe(repo) -> None:
    put_pipe(repo, "LOAD_ORDERS")
    put_pipe(repo, "LOAD_CUSTOMERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
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
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
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
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 1)).plain == ""
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""
        assert table.get_cell_at((0, 6)).plain == ""


def test_pipe_list_view_scopes_includes_the_activity_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    view = PipeListView(repo, scope)
    assert pipe_activity_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_pipe_row_opens_it(repo) -> None:
    """See test_pressing_enter_on_a_table_row_opens_it -- same handler,
    same reasoning."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
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
    assert app.opened == [ObjectRef(ObjectType.PIPE, "MYDB", "PUBLIC", "LOAD_ORDERS")]


async def test_the_activity_tab_says_its_columns_lag_behind(repo) -> None:
    """Every Activity column but Name comes from ACCOUNT_USAGE. The line sits
    in that tab only: the Status tab's columns do not lag."""
    put_pipe(repo, "LOAD_ORDERS")
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        banner = pane.query_one("#tab-activity").query_one("#detail-lag-note")
        assert str(banner.render()) == (
            "Load activity comes from ACCOUNT_USAGE, which Snowflake says can "
            "be up to 2 hours behind."
        )
        assert not pane.query_one("#tab-status").query("#detail-lag-note")


async def test_the_status_tab_shows_pending_files_and_the_last_file_ingested(
    repo,
) -> None:
    """Both come with the execution state, so they cost no query of their
    own. Last Ingested is when the pipe received its newest file, loaded or
    failed, near real time unlike the Activity tab; a pipe that never
    received one has none, and its cell stays blank."""
    put_pipe(repo, "LOAD_CUSTOMERS")
    put_pipe(repo, "LOAD_ORDERS")
    load(
        repo,
        pipe_status_scope("MYDB", "PUBLIC"),
        [
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "LOAD_CUSTOMERS",
                "PIPE_STATUS": '{"executionState":"RUNNING","pendingFileCount":0}',
            },
            {
                "PIPE_CATALOG_NAME": "MYDB",
                "PIPE_SCHEMA_NAME": "PUBLIC",
                "PIPE_NAME": "LOAD_ORDERS",
                "PIPE_STATUS": (
                    '{"executionState":"RUNNING","pendingFileCount":1234,'
                    '"lastIngestedTimestamp":"2026-11-03T20:11:26.046Z",'
                    '"lastIngestedFilePath":"bad_1.csv"}'
                ),
            },
        ],
    )
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(120, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-status", DataTable)
        assert table.get_cell_at((0, 3)).plain == "0"
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((1, 3)).plain == "1,234"
        assert table.get_cell_at((1, 4)).plain == format_timestamp_minutes(
            "2026-11-03T20:11:26.046Z"
        )
