# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""PipeView: one pipe's load history and SQL."""

from __future__ import annotations

from datetime import datetime, timedelta

from textual.widgets import DataTable, Static, TabbedContent, TextArea

from nivaria.models import (
    ObjectRef,
    ObjectType,
    pipe_activity_history_scope,
    pipe_activity_scope,
    pipe_history_scope,
)
from nivaria.settings import PIPE_HISTORY_SHOW_DAYS, config
from nivaria.ui.views.pipe_view import PipeView
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    load,
    put_pipe,
    put_pipe_activity,
    put_pipe_activity_history,
    put_pipe_history,
)


async def test_a_pipe_shows_load_history_not_columns(repo) -> None:
    """A different shape entirely -- which is what the registry is for."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-history", DataTable)
        assert len(pane.query("#detail-columns")) == 0


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


async def test_a_pipe_with_no_target_never_requests_history_or_errors(
    repo,
) -> None:
    """A pipe with no cached activity data and an unqualified target in
    its definition has no target table to resolve (see
    Repository.pipe_target_table). That must not repeatedly fail a fetch it
    can never succeed at: history_scope is simply never requested until a
    target becomes resolvable, and the view shows an empty table instead of
    an error."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-history", DataTable).row_count == 0
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
        assert pane.query_one("#detail-history", DataTable).row_count == 0


def _days_ago(days: float) -> str:
    """A relative, always-recent-enough timestamp -- these tests filter by
    the show window against the real current time, so a fixed calendar date
    would eventually age out of it."""
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat()


async def test_a_pipes_history_table_lists_rows_from_both_sources(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")  # resolves the target table
    put_pipe_activity_history(
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
        table = pane.query_one("#detail-history", DataTable)
        assert table.row_count == 2
        # Sorted newest first.
        assert table.get_cell_at((0, 4)).plain == "orders_9.csv.gz"
        assert table.get_cell_at((1, 4)).plain == "orders_1.csv.gz"


async def test_a_pipes_history_ignores_another_pipes_activity_rows(repo) -> None:
    """account_usage_copy_history_detail is scoped per pipe -- a row cached
    for another pipe must never leak into this one's history table."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity_history(repo, "LOAD_OTHER", FILE_NAME="other.csv.gz")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-history", DataTable).row_count == 0


async def test_the_near_real_time_source_wins_on_an_overlapping_row(repo) -> None:
    """Merged by (FILE_NAME, PIPE_RECEIVED_TIME) -- the near-real-time source
    is inserted last, so the same load from the backfill is overlaid rather
    than duplicated."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    when = _days_ago(10)
    received = _days_ago(10.001)
    put_pipe_activity(repo, "LOAD_ORDERS")  # resolves the target table
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=when,
        PIPE_RECEIVED_TIME=received,
        STATUS="Load in progress",
    )
    put_pipe_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=when,
        PIPE_RECEIVED_TIME=received,
        STATUS="Loaded",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-history", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 3)).plain == "Loaded"


async def test_the_same_load_is_shown_once_even_when_the_sources_disagree_on_its_load_time(
    repo,
) -> None:
    """The two views stamp LAST_LOAD_TIME independently -- these offsets are
    the ones a production account reported for one load (AU 11:11:24.961,
    IS 11:11:24.926, both received 11:11:05.757). Keying on LAST_LOAD_TIME
    showed that load twice; PIPE_RECEIVED_TIME is what both agree on."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    received = datetime.now().astimezone() - timedelta(days=1)
    put_pipe_activity(repo, "LOAD_ORDERS")  # resolves the target table
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="load_20260930110515.csv",
        LAST_LOAD_TIME=(received + timedelta(seconds=19.204)).isoformat(),
        PIPE_RECEIVED_TIME=received.isoformat(),
        STATUS="Load in progress",
    )
    put_pipe_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="load_20260930110515.csv",
        LAST_LOAD_TIME=(received + timedelta(seconds=19.169)).isoformat(),
        PIPE_RECEIVED_TIME=received.isoformat(),
        STATUS="Loaded",
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-history", DataTable)
        assert table.row_count == 1
        assert table.get_cell_at((0, 3)).plain == "Loaded"


async def test_two_loads_of_the_same_file_name_are_both_shown(repo) -> None:
    """Keying on FILE_NAME alone would be simpler and would hide a file that
    was genuinely ingested twice -- e.g. overwritten on the stage and picked
    up again -- which is exactly the history worth seeing."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")  # resolves the target table
    put_pipe_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=_days_ago(2),
        PIPE_RECEIVED_TIME=_days_ago(2.001),
    )
    put_pipe_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="orders_1.csv.gz",
        LAST_LOAD_TIME=_days_ago(1),
        PIPE_RECEIVED_TIME=_days_ago(1.001),
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-history", DataTable).row_count == 2


async def test_rows_older_than_the_show_window_are_hidden_but_stay_cached(repo) -> None:
    """The display window can be shorter than the fetch window on purpose --
    shrinking it is a display-only change, since the wider range is still in
    the cache either way (see settings.PIPE_HISTORY_SHOW_DAYS)."""
    config.set(PIPE_HISTORY_SHOW_DAYS, 30)
    ref = put_pipe(repo, "LOAD_ORDERS")
    old = datetime.now().astimezone() - timedelta(days=31)
    put_pipe_activity_history(
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
        assert pane.query_one("#detail-history", DataTable).row_count == 0
    # Still in the cache -- only hidden from this paint.
    assert repo.raw_rows(pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"))


async def test_the_highlighted_rows_error_shows_in_the_history_detail_panel(
    repo,
) -> None:
    """Too long for a table column -- shown below the table instead, for
    whichever row the cursor is currently on."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_activity_history(
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
        panel = pane.query_one("#detail-history-item", Static)
        assert panel.display
        assert "Line 42" in str(panel.render())
        assert "Numeric value 'x' is not recognized" in str(panel.render())


async def test_a_row_with_no_error_leaves_the_history_detail_panel_hidden(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=_days_ago(1))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        panel = pane.query_one("#detail-history-item", Static)
        assert not panel.display


async def test_highlighting_a_different_history_row_updates_the_detail_panel(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_activity_history(
        repo,
        "LOAD_ORDERS",
        FILE_NAME="bad.csv.gz",
        LAST_LOAD_TIME=_days_ago(1),
        FIRST_ERROR_MESSAGE="boom",
        FIRST_ERROR_LINE_NUMBER=1,
    )
    put_pipe_activity_history(
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
        table = pane.query_one("#detail-history", DataTable)
        panel = pane.query_one("#detail-history-item", Static)
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
    put_pipe_activity(repo, "LOAD_ORDERS")
    when = _days_ago(1)
    put_pipe_activity_history(
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
        assert pane.query_one("#detail-history-item", Static).display

        # Same file/time key, now with no error -- overwrites the row.
        put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=when)
        pane.cache_updated(pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"))
        await pilot.pause()
        assert not pane.query_one("#detail-history-item", Static).display


async def test_the_sql_tab_shows_the_pipes_raw_sql(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS", definition="COPY INTO t FROM @s")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-sql", TextArea).text == "COPY INTO t FROM @s"


async def test_the_sql_tab_falls_back_when_uncached(repo) -> None:
    ref = ObjectRef(ObjectType.PIPE, "MYDB", "PUBLIC", "GHOST")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-sql", TextArea).text == "-- not cached"


async def test_the_tab_mnemonic_actions_switch_tabs(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
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


async def test_a_capped_history_says_so_above_the_table(repo) -> None:
    """A history table has no natural bottom -- it just stops -- so a capped
    one is indistinguishable from a complete one by looking at it. That is the
    one thing this app is not allowed to do: present a partial answer as if it
    were whole."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_activity_history(repo, "LOAD_ORDERS")
    load(
        repo,
        pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
        [
            {k: v for k, v in row.items() if not k.startswith("_")}
            for row in repo.raw_rows(
                pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS")
            )
        ],
        truncated=True,
    )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        note = app.query_one("#detail-history-note", Static)
        assert note.display
        assert "history row cap" in str(note.render())


async def test_an_uncapped_history_shows_no_note(repo) -> None:
    """The common case. A note on every pipe would be noise, and noise is how
    a real warning gets ignored."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    # Within the show window -- see _days_ago's docstring: a fixed calendar
    # date would eventually age out and start looking like an empty history.
    put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=_days_ago(1))
    load(repo, pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        assert not app.query_one("#detail-history-note", Static).display


async def test_a_history_whose_refresh_failed_says_its_rows_are_older(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=_days_ago(1))
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS")
    repo.fail_pass(scope, repo.begin_pass(scope), "No active warehouse selected")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        note = app.query_one("#detail-history-note", Static)
        assert note.display
        assert note.has_class("-error")
        assert str(note.render()).endswith(
            "the refresh failed: No active warehouse selected"
        )


async def test_a_new_load_arriving_keeps_the_highlighted_load(repo) -> None:
    """Rows used to be keyed by position, so a newer load at the top moved
    every key along -- and a repaint put the cursor back on the first row
    regardless, losing the error the user was reading."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    for hours, name in ((3, "old.csv"), (2, "mid.csv"), (1, "new.csv")):
        put_pipe_activity_history(
            repo,
            "LOAD_ORDERS",
            FILE_NAME=name,
            LAST_LOAD_TIME=(
                datetime.now().astimezone() - timedelta(hours=hours)
            ).isoformat(),
        )
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        table = pane.query_one("#detail-history", DataTable)
        table.move_cursor(row=1)
        await pilot.pause()
        assert table.get_cell_at((table.cursor_row, 4)).plain == "mid.csv"

        put_pipe_activity_history(
            repo,
            "LOAD_ORDERS",
            FILE_NAME="newest.csv",
            LAST_LOAD_TIME=datetime.now().astimezone().isoformat(),
        )
        pane.cache_updated(pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"))
        await pilot.pause()

        assert table.row_count == 4
        assert table.get_cell_at((table.cursor_row, 4)).plain == "mid.csv"


async def test_the_s_key_shows_the_pipes_sql(repo) -> None:
    """`d` means Data on tables and views, so a pipe's SQL is `s` too."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        tabs = view.query_one(TabbedContent)
        # Bindings reach the view only through the focus chain.
        pane.query_one("#detail-history", DataTable).focus()
        await pilot.press("s")
        assert tabs.active == "tab-sql"
        await pilot.press("h")
        assert tabs.active == "tab-history"


#: A definition naming its target in full, so the target resolves without a
#: PIPE_ACTIVITY row -- see pipe_definition.copy_target.
_QUALIFIED = "COPY INTO mydb.raw.orders FROM @s"


def _note(app) -> Static | None:
    note = app.query_one("#detail-history-note", Static)
    return note if note.display else None


async def test_a_pipe_the_snapshot_saw_idle_asks_for_no_backfill(repo) -> None:
    """The backfill reads the same view over the same window as the
    snapshot, and could only repeat its "nothing"."""
    ref = put_pipe(repo, "LOAD_ORDERS", definition=_QUALIFIED)
    put_pipe_activity(repo, "LOAD_OTHER")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        note = _note(app)
        assert note is not None
        assert str(note.render()) == "Looking for load activity..."
    backfill = pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS")
    assert backfill.key not in app.needed
    assert backfill not in PipeView(repo, ref).scopes()


async def test_a_pipe_with_no_snapshot_yet_asks_for_no_backfill(repo) -> None:
    """Whether the backfill can add anything is only known once the
    snapshot has answered."""
    ref = put_pipe(repo, "LOAD_ORDERS", definition=_QUALIFIED)
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    backfill = pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS")
    assert backfill.key not in app.needed


async def test_a_pipe_the_snapshot_saw_loading_asks_for_its_backfill(repo) -> None:
    """Its older loads are only in the backfill."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    backfill = pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS")
    assert backfill.key in app.needed


async def test_an_idle_pipe_is_answered_once_its_14_day_history_is(repo) -> None:
    """The snapshot covers the window up to its last refresh, at most one cache
    age ago, and the 14-day history from there to now."""
    ref = put_pipe(repo, "LOAD_ORDERS", definition=_QUALIFIED)
    put_pipe_activity(repo, "LOAD_OTHER")
    load(repo, pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        note = _note(app)
        assert note is not None
        assert str(note.render()) == "No load activity in the last 90 days."


async def test_a_pipe_with_nothing_known_yet_says_it_is_looking(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        note = _note(app)
        assert note is not None
        assert str(note.render()) == "Looking for load activity..."


async def test_a_recent_load_shows_while_the_backfill_still_runs(repo) -> None:
    """Rows from the near-real-time source alone look like the whole
    history; the note says they are not."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=_days_ago(2))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        note = _note(app)
        assert note is not None
        assert str(note.render()) == (
            "Showing the last 14 days. Still looking for older load activity..."
        )


async def test_a_cached_history_being_refreshed_stays_quiet(repo) -> None:
    """A refresh keeps the cached rows, so a revisit has its answer already
    and says nothing about looking."""
    ref = put_pipe(repo, "LOAD_ORDERS", definition=_QUALIFIED)
    put_pipe_activity(repo, "LOAD_ORDERS")
    put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=_days_ago(1))
    load(repo, pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"), [])
    repo.begin_pass(pipe_activity_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        assert _note(app) is None


async def test_a_cached_empty_history_being_refreshed_keeps_its_answer(repo) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS", definition=_QUALIFIED)
    put_pipe_activity(repo, "LOAD_OTHER")
    load(repo, pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"), [])
    repo.begin_pass(pipe_activity_scope("MYDB", "PUBLIC"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        note = _note(app)
        assert note is not None
        assert str(note.render()) == "No load activity in the last 90 days."


async def test_a_never_loaded_pipe_with_a_qualified_target_asks_for_its_history(
    repo,
) -> None:
    """The definition names the table, so the 14-day query runs on the
    cache-age cadence like any loaded pipe's."""
    ref = put_pipe(repo, "LOAD_ORDERS", definition=_QUALIFIED)
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS").key in app.needed


async def test_a_never_loaded_pipe_answers_from_its_14_day_history_first(
    repo,
) -> None:
    ref = put_pipe(repo, "LOAD_ORDERS", definition=_QUALIFIED)
    load(repo, pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        note = _note(app)
        assert note is not None
        assert str(note.render()) == (
            "No load activity in the last 14 days. Still looking..."
        )


def _lag_banner(app) -> str | None:
    banner = app.query_one("#detail-lag-note", Static)
    return str(banner.render()) if banner.display else None


async def test_a_pipe_without_a_known_target_shows_the_lag_banner(repo) -> None:
    """Only ACCOUNT_USAGE speaks for such a pipe, so its newest load can be
    missing for a while; the wording quotes Snowflake's figure without
    promising a maximum."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_OTHER")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        assert _lag_banner(app) == (
            "Load activity comes from ACCOUNT_USAGE, which Snowflake says can "
            "be up to 2 hours behind."
        )
        note = _note(app)
        assert note is not None
        assert str(note.render()) == "No load activity in the last 90 days."


async def test_a_pipe_with_a_known_target_shows_no_lag_banner(repo) -> None:
    """Its recent days come from INFORMATION_SCHEMA, near real time."""
    ref = put_pipe(repo, "LOAD_ORDERS", definition=_QUALIFIED)
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        assert _lag_banner(app) is None


async def test_a_pipe_whose_load_names_no_target_still_shows_its_history(
    repo,
) -> None:
    """A load whose row names no target table leaves the target unknown, so
    the backfill alone shows the history, under the lag banner."""
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_ORDERS", TABLE_NAME=None)
    put_pipe_activity_history(repo, "LOAD_ORDERS", LAST_LOAD_TIME=_days_ago(1))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        assert app.query_one(PipeView).query_one(DataTable).row_count == 1
        assert _lag_banner(app) is not None
