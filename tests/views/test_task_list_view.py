"""TaskListView: every task in a schema."""

from __future__ import annotations

from textual.widgets import DataTable, TabbedContent

from nivaria.models import (
    ObjectRef,
    ObjectType,
    objects_scope,
    task_activity_scope,
)
from nivaria.ui.views.base import registered_category_types
from nivaria.ui.views.task_list_view import TaskListView
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_table,
    put_task,
    put_task_activity,
)


async def test_selecting_a_tasks_category_mounts_the_task_list_view(repo) -> None:
    put_task(repo, "REFRESH_ORDERS")
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert isinstance(pane.view, TaskListView)


async def test_the_task_list_view_has_no_tabs(repo) -> None:
    """Explicitly not tabbed like PipeListView -- there is only one thing to
    show for a schema's tasks right now."""
    put_task(repo, "REFRESH_ORDERS")
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert len(pane.query(TabbedContent)) == 0


async def test_the_task_list_view_lists_every_cached_task(repo) -> None:
    put_task(repo, "REFRESH_ORDERS", warehouse="MYWH", schedule="5 MINUTE")
    put_task(repo, "REFRESH_TOTALS", warehouse="OTHERWH", schedule="1 HOUR")
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.row_count == 2
        assert table.get_cell_at((0, 0)).plain == "REFRESH_ORDERS"
        assert table.get_cell_at((0, 1)).plain == "MYWH"
        assert table.get_cell_at((0, 2)).plain == "5 MINUTE"
        assert table.get_cell_at((1, 0)).plain == "REFRESH_TOTALS"


async def test_the_state_column_shows_snowflakes_own_wording_unmodified(repo) -> None:
    """DESIGN.md's "mirror Snowflake, don't reinterpret it" rule -- the raw
    show_tasks.state value is shown verbatim, not relabelled."""
    put_task(repo, "REFRESH_ORDERS", state="suspended")
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.get_cell_at((0, 3)).plain == "suspended"


async def test_state_and_last_run_state_stay_visually_distinct(repo) -> None:
    """A suspended-but-last-succeeded task and an enabled-but-last-failed task
    must be distinguishable -- State (show_tasks.state) and Last Run State
    (TASK_HISTORY.STATE) are two columns Snowflake genuinely both names
    `state`, showing different things, not the same field twice."""
    put_task(repo, "REFRESH_ORDERS", state="suspended")
    put_task_activity(repo, "REFRESH_ORDERS", STATE="SUCCEEDED")
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.get_cell_at((0, 3)).plain == "suspended"
        assert table.get_cell_at((0, 5)).plain == "SUCCEEDED"


async def test_task_activity_columns_render_when_the_scope_has_data(repo) -> None:
    put_task(repo, "REFRESH_ORDERS")
    put_task_activity(
        repo,
        "REFRESH_ORDERS",
        SCHEDULED_TIME="2026-03-01 08:00:00",
        STATE="FAILED",
    )
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.get_cell_at((0, 4)).plain == "2026-03-01 08:00"
        assert table.get_cell_at((0, 5)).plain == "FAILED"


async def test_task_activity_columns_are_blank_without_a_successful_activity_pass(
    repo,
) -> None:
    """A missing/failed activity scope must not block the SHOW-sourced
    columns: the row still renders, just with the activity cells blank."""
    put_task(repo, "REFRESH_ORDERS")
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        table = pane.query_one("#detail-list", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 4)).plain == ""
        assert table.get_cell_at((0, 5)).plain == ""


def test_task_list_view_scopes_includes_the_activity_scope(repo) -> None:
    """This is what wires the new scope into the background staleness sweep
    (MainScreen._sweep reads self.detail.scopes()) with no other plumbing."""
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    view = TaskListView(repo, scope)
    assert task_activity_scope("MYDB", "PUBLIC") in view.scopes()


async def test_pressing_enter_on_a_task_row_opens_it(repo) -> None:
    """See test_pressing_enter_on_a_table_row_opens_it -- same handler,
    same reasoning."""
    put_task(repo, "REFRESH_ORDERS")
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
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
    assert app.opened == [
        ObjectRef(ObjectType.TASK, "MYDB", "PUBLIC", "REFRESH_ORDERS")
    ]


async def test_every_category_kind_has_a_list_view() -> None:
    """Tables, Views, Pipes, Stages and Tasks -- every ObjectType the tree
    produces a category node for -- must have a registered CategoryView, so
    `show_category`'s placeholder fallback is only ever defensive, never hit
    in practice for a real type."""
    assert {
        ObjectType.TABLE,
        ObjectType.VIEW,
        ObjectType.PIPE,
        ObjectType.STAGE,
        ObjectType.TASK,
    } <= registered_category_types()


async def test_a_category_cache_update_repaints_the_list(repo) -> None:
    scope = objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show_category(scope)
        await pilot.pause()
        assert pane.query_one("#detail-list", DataTable).row_count == 0

        put_table(repo, "ORDERS")
        pane.cache_updated(scope)
        await pilot.pause()
        assert pane.query_one("#detail-list", DataTable).row_count == 1
