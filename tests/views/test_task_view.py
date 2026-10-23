"""TaskView: one task's run history and SQL."""

from __future__ import annotations

from datetime import datetime, timedelta

from textual.widgets import DataTable, Static, TabbedContent, TextArea

from nivaria.models import (
    ObjectRef,
    ObjectType,
    task_activity_history_scope,
    task_history_scope,
)
from nivaria.settings import TASK_HISTORY_SHOW_DAYS, config
from nivaria.ui.views.task_view import TaskView
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_task,
    put_task_activity_history,
    put_task_history,
)


async def test_a_task_shows_run_history_not_columns(repo) -> None:
    """A different shape entirely -- which is what the registry is for."""
    ref = put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-history", DataTable)
        assert len(pane.query("#detail-columns")) == 0


async def test_opening_a_task_requests_both_history_scopes_unconditionally(
    repo,
) -> None:
    """Unlike PipeView, there is no target-table resolution gate here: both
    task_activity_history_scope and task_history_scope are requested the
    moment the view opens, since DATABASE_NAME/SCHEMA_NAME/TASK_NAME already
    come straight off this task's own ref."""
    ref = put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS").key in (
        app.needed
    )
    assert task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS").key in app.needed


async def test_a_tasks_history_table_is_empty_with_nothing_cached(repo) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-history", DataTable).row_count == 0


def _days_ago_iso(days: float) -> str:
    """Same relative-timestamp trick as pipe view's _days_ago -- these tests
    filter by the show window against the real current time."""
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat()


async def test_a_tasks_history_table_lists_rows_from_both_sources(repo) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-old",
        SCHEDULED_TIME=_days_ago_iso(60),
    )
    put_task_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-new",
        SCHEDULED_TIME=_days_ago_iso(5),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-history", DataTable)
        assert table.row_count == 2
        # Sorted newest first.
        assert table.get_cell_at((0, 3)).plain == "SUCCEEDED"


async def test_a_tasks_history_ignores_another_tasks_activity_rows(repo) -> None:
    """account_usage_task_history_detail is scoped per task -- a row cached
    for another task must never leak into this one's history table."""
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(repo, "REFRESH_OTHER", QUERY_ID="q-other")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-history", DataTable).row_count == 0


async def test_the_near_real_time_source_wins_on_an_overlapping_query_id(repo) -> None:
    """Merged by QUERY_ID alone -- the near-real-time source is inserted
    last, so an identical id from the backfill is overlaid rather than
    duplicated."""
    ref = put_task(repo, "REFRESH_ORDERS")
    when = _days_ago_iso(10)
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-1",
        SCHEDULED_TIME=when,
        STATE="EXECUTING",
    )
    put_task_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-1",
        SCHEDULED_TIME=when,
        STATE="SUCCEEDED",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-history", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 3)).plain == "SUCCEEDED"


async def test_rows_older_than_the_task_show_window_are_hidden_but_stay_cached(
    repo,
) -> None:
    """Same display-vs-fetch-window split as pipe view -- see settings.
    TASK_HISTORY_SHOW_DAYS."""
    config.set(TASK_HISTORY_SHOW_DAYS, 30)
    ref = put_task(repo, "REFRESH_ORDERS")
    old = datetime.now().astimezone() - timedelta(days=31)
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-ancient",
        SCHEDULED_TIME=old.isoformat(),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-history", DataTable).row_count == 0
    # Still in the cache -- only hidden from this paint.
    assert repo.raw_rows(
        task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS")
    )


async def test_the_highlighted_rows_error_shows_in_the_task_history_detail_panel(
    repo,
) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        SCHEDULED_TIME=_days_ago_iso(1),
        ERROR_CODE=100132,
        ERROR_MESSAGE="Numeric value 'x' is not recognized",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#detail-history-item", Static)
        assert panel.display
        assert "100132" in str(panel.render())
        assert "Numeric value 'x' is not recognized" in str(panel.render())


async def test_a_task_row_with_no_error_leaves_the_history_detail_panel_hidden(
    repo,
) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(repo, "REFRESH_ORDERS", SCHEDULED_TIME=_days_ago_iso(1))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#detail-history-item", Static)
        assert not panel.display


async def test_highlighting_a_different_task_history_row_updates_the_detail_panel(
    repo,
) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-bad",
        SCHEDULED_TIME=_days_ago_iso(1),
        ERROR_CODE=1,
        ERROR_MESSAGE="boom",
    )
    put_task_activity_history(
        repo,
        "REFRESH_ORDERS",
        QUERY_ID="q-good",
        SCHEDULED_TIME=_days_ago_iso(2),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-history", DataTable)
        panel = pane.query_one("#detail-history-item", Static)
        # Newest first: row 0 is "q-bad", the errored one.
        assert panel.display
        table.move_cursor(row=1)
        await pilot.pause()
        assert not panel.display
        table.move_cursor(row=0)
        await pilot.pause()
        assert panel.display


async def test_the_sql_tab_shows_the_tasks_raw_sql(repo) -> None:
    ref = put_task(repo, "REFRESH_ORDERS", definition="INSERT INTO t SELECT * FROM s")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert (
            pane.query_one("#detail-sql", TextArea).text
            == "INSERT INTO t SELECT * FROM s"
        )


async def test_the_task_sql_tab_falls_back_when_uncached(repo) -> None:
    ref = ObjectRef(ObjectType.TASK, "MYDB", "PUBLIC", "GHOST")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-sql", TextArea).text == "-- not cached"


async def test_the_task_tab_mnemonic_actions_switch_tabs(repo) -> None:
    ref = put_task(repo, "REFRESH_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        assert tabs.active == "tab-history"
        view.action_show_tab("sql")
        assert tabs.active == "tab-sql"
        view.action_show_tab("history")
        assert tabs.active == "tab-history"


def test_task_view_scopes_always_includes_all_three_task_scopes(repo) -> None:
    """Unlike PipeView, nothing here is gated on a resolved target -- see the
    module docstring's reasoning."""
    ref = put_task(repo, "REFRESH_ORDERS")
    view = TaskView(repo, ref)
    assert task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS") in (
        view.scopes()
    )
    assert task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS") in view.scopes()
