# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import asyncio
import threading

from tcoda.widgets import FooterBinding
from textual.widgets import Input

from nivaria import paths
from nivaria.cache.db import Database
from nivaria.cache.repository import Repository
from nivaria.models import (
    databases_scope,
    grants_scope,
    pipe_history_scope,
    roles_scope,
    tag_references_scope,
    warehouses_scope,
)
from nivaria.snowflake.pool import LOGIN_TIMED_OUT
from nivaria.ui.app import NivariaApp
from nivaria.ui.screens.closing import ClosingDialog
from nivaria.ui.screens.connect import ConnectScreen
from nivaria.ui.screens.main import MainScreen
from tests.fakes import FakeConnector


async def settle(pilot, rounds: int = 4) -> None:
    for _ in range(rounds):
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()


_GONE = "SQL compilation error:\nSchema 'MYDB.GONE' does not exist or not authorized."


async def test_a_dropped_object_is_reported_as_a_warning_not_an_error() -> None:
    """The fetch service is already clearing it out of the tree; nothing is
    broken that the user has to act on."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._report_fetch_failure(grants_scope("MYDB", "GONE"), _GONE, True)
        app._report_fetch_failure(databases_scope(), "network down", False)
        await pilot.pause()
        toasts = [(toast.severity, toast.message) for toast in app._notifications]
    assert toasts == [
        (
            "warning",
            "Schema 'MYDB.GONE' does not exist or not authorized. Refreshing.",
        ),
        ("error", "Could not load databases: network down"),
    ]


async def test_a_pipes_missing_target_table_raises_no_toast() -> None:
    """A lasting state, retried every cache age and reported by the pipe's
    History note; a toast would come back each time."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._report_fetch_failure(
            pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
            "SQL compilation error:\nTable 'MYDB.RAW.ORDERS' does not exist or "
            "not authorized.",
            True,
        )
        await pilot.pause()
        assert list(app._notifications) == []


async def test_one_dropped_object_failing_several_scopes_toasts_once() -> None:
    """Opening a dropped schema fails its Grants and its Tags together."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._report_fetch_failure(grants_scope("MYDB", "GONE"), _GONE, True)
        app._report_fetch_failure(tag_references_scope("MYDB", "GONE"), _GONE, True)
        await pilot.pause()
        assert len(list(app._notifications)) == 1


async def test_a_prompt_nobody_answered_toasts_once_and_says_what_to_do() -> None:
    """One unanswered prompt fails every scope waiting on it. The user needs to
    hear about the login once, not about each scope."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._report_fetch_failure(databases_scope(), LOGIN_TIMED_OUT, False)
        app._report_fetch_failure(
            grants_scope("MYDB", "PUBLIC"), LOGIN_TIMED_OUT, False
        )
        await pilot.pause()
        toasts = [(toast.severity, toast.message) for toast in app._notifications]
    text = "Login timed out: the browser login did not finish in time."
    assert toasts == [("warning", f"{text} Press F5 to be asked again.")]


async def test_the_app_opens_the_connect_screen_first() -> None:
    """Nothing can be browsed before a pool exists, and opening that pool is
    where the MFA prompts happen."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, ConnectScreen)


async def test_connecting_hands_over_to_the_browser() -> None:
    """The default pool size is more than one, so this also exercises
    confirming the extra connections on the way to the browser."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        screen.query_one("#account", Input).value = "ACME"
        screen.query_one("#user", Input).value = "me"
        screen.submit()
        await settle(pilot)
        await pilot.press("enter")  # confirm opening the additional connections
        await settle(pilot)
        assert isinstance(pilot.app.screen, MainScreen)
        assert app.pool is not None
        assert app.fetch is not None


async def test_the_connect_chord_reopens_the_connect_dialog() -> None:
    """Get back to the connect dialog without restarting the app, e.g. to
    switch accounts mid-session."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        screen.query_one("#account", Input).value = "ACME"
        screen.query_one("#user", Input).value = "me"
        screen.submit()
        await settle(pilot)
        await pilot.press("enter")  # confirm opening the additional connections
        await settle(pilot)
        assert isinstance(pilot.app.screen, MainScreen)

        await pilot.press("comma", "a", "c")
        await settle(pilot)
        assert isinstance(pilot.app.screen, ConnectScreen)


class RecordingFetch:
    """A minimal FetchService stand-in that records when, and against which
    screen state, it was told to stop -- used to prove
    NivariaApp.open_connect fully quiesces the outgoing identity before
    MainScreen is repointed at a new one, the same discipline
    MainScreen._switch_role already uses for a role switch (see docs/DESIGN.md).
    """

    def __init__(self, app: NivariaApp) -> None:
        self._app = app
        self.stopped = False
        #: Whether the MainScreen still on the stack (possibly under a modal)
        #: still had `.fetch is self` at the moment stop() ran -- False would
        #: mean the screen had already been repointed at a successor before
        #: this one finished quiescing.
        self.screen_still_pointed_here_when_stopped: bool | None = None
        self.requested: list = []
        #: Statements "in flight"; quitting waits for this to reach 0.
        self.running_count = 0

    def request(self, scope, priority) -> bool:
        self.requested.append(scope)
        return True

    def cancel_where(self, predicate) -> list:
        return []

    def is_busy(self, scope) -> bool:
        return False

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 2.0) -> None:
        main_screens = [s for s in self._app.screen_stack if isinstance(s, MainScreen)]
        self.screen_still_pointed_here_when_stopped = bool(
            main_screens and main_screens[-1].fetch is self
        )
        self.stopped = True


def _use_recording_fetch(app: NivariaApp) -> None:
    """Swap FetchService for RecordingFetch on this instance, so a test can
    observe reconnect ordering/timing with no real worker threads.

    An instance attribute, not a subclass: Textual resolves an inherited
    `CSS_PATH` relative to whichever module actually defines the class, so a
    `NivariaApp` subclass declared in this test file would otherwise go
    looking for `app.tcss` next to this file instead of the real one.
    """
    app._make_fetch = lambda pool: RecordingFetch(app)  # type: ignore[method-assign]


async def _connect(pilot, account: str) -> None:
    """Drive the connect form to completion, including confirming pool
    growth -- the default pool size is 3, so every connect here goes
    through the growth-confirmation dialog."""
    screen = pilot.app.screen
    screen.query_one("#account", Input).value = account
    screen.query_one("#user", Input).value = "me"
    screen.submit()
    await settle(pilot)
    await pilot.press("enter")
    await settle(pilot)


async def _reopen_connect(pilot) -> None:
    await pilot.press("comma", "a", "c")
    await settle(pilot)


async def test_reconnecting_stops_the_old_fetch_before_the_screen_points_at_a_new_one() -> (
    None
):
    """Regression: open_connect used to repoint MainScreen.fetch at the new
    FetchService before stopping the old one. A ScopeNeeded message from the
    old profile's tree still in flight while the connect dialog was open
    could then be resolved against the *new* identity's pool after the
    swap -- firing SQL for a database that may not even exist in the new
    account. Stopping the old fetch first, the way a role switch already
    does, closes that window."""
    app = NivariaApp(connector=FakeConnector())
    _use_recording_fetch(app)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _connect(pilot, "TEST")
        assert isinstance(pilot.app.screen, MainScreen)
        first_fetch = app.fetch
        assert isinstance(first_fetch, RecordingFetch)

        await _reopen_connect(pilot)
        await _connect(pilot, "PROD")

        assert isinstance(pilot.app.screen, MainScreen)
        assert first_fetch.stopped
        assert first_fetch.screen_still_pointed_here_when_stopped is True
        assert app.fetch is not first_fetch


async def test_reconnecting_shows_a_please_wait_dialog_while_the_old_session_closes() -> (
    None
):
    """FetchService.stop() joins worker threads that cannot be interrupted
    mid-query, so it can take a couple of seconds -- long enough to warrant
    its own progress dialog, the same idea as the "Connecting..." one shown
    while opening a new connection.

    Not asserted by catching the dialog on screen mid-reconnect: the pilot's
    press()/pause() wait for the app to reach full idle, which includes the
    `open_connect` worker and the asyncio.to_thread call inside it, so by the
    time either returns the whole reconnect -- dialog shown and dismissed
    again -- has already happened. Recording every screen open_connect
    pushes proves it happened without depending on catching one exact
    frame."""
    app = NivariaApp(connector=FakeConnector())
    _use_recording_fetch(app)
    pushed: list[type] = []
    real_push_screen = app.push_screen

    def recording_push_screen(screen, *args, **kwargs):
        pushed.append(type(screen))
        return real_push_screen(screen, *args, **kwargs)

    app.push_screen = recording_push_screen  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _connect(pilot, "TEST")
        first_fetch = app.fetch
        assert isinstance(first_fetch, RecordingFetch)

        await _reopen_connect(pilot)
        await _connect(pilot, "PROD")

        assert isinstance(pilot.app.screen, MainScreen)
        assert first_fetch.stopped

    assert ClosingDialog in pushed


async def test_reconnecting_prefetches_the_new_identitys_databases_warehouses_and_roles() -> (
    None
):
    """Same as at startup and after a role switch: the new identity's cache
    file may have nothing in it yet, and without asking up front the first
    open of each picker only requested the list and closed again."""
    app = NivariaApp(connector=FakeConnector())
    _use_recording_fetch(app)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _connect(pilot, "TEST")
        first_fetch = app.fetch

        await _reopen_connect(pilot)
        await _connect(pilot, "PROD")

        assert isinstance(pilot.app.screen, MainScreen)
        second_fetch = app.fetch
        assert isinstance(second_fetch, RecordingFetch)
        assert second_fetch is not first_fetch
        for scope in (databases_scope(), warehouses_scope(), roles_scope()):
            assert scope in second_fetch.requested


async def test_quitting_the_connect_screen_exits() -> None:
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
    assert app.pool is None


async def test_quitting_mid_fetch_waits_behind_a_dialog_until_the_fetch_returns() -> (
    None
):
    """Regression: a fetch worker that outlived the app hopped back into it
    and raised "App is not running". Its statement cannot be interrupted, so
    quit waits it out, visibly."""
    app = NivariaApp(connector=FakeConnector())
    _use_recording_fetch(app)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _connect(pilot, "TEST")
        fetch = app.fetch
        assert isinstance(fetch, RecordingFetch)
        fetch.running_count = 1

        await pilot.press("ctrl+q")
        await settle(pilot)
        assert isinstance(app.screen, ClosingDialog)
        assert fetch.stopped
        assert app.is_running

        fetch.running_count = 0
        await settle(pilot)
        assert app.is_running is False


async def test_a_second_quit_while_waiting_exits_at_once() -> None:
    """A long query should not hold the app hostage."""
    app = NivariaApp(connector=FakeConnector())
    _use_recording_fetch(app)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _connect(pilot, "TEST")
        assert isinstance(app.fetch, RecordingFetch)
        app.fetch.running_count = 1

        await pilot.press("ctrl+q")
        await settle(pilot)
        assert app.is_running
        await pilot.press("ctrl+q")
        await settle(pilot)
        assert app.is_running is False


async def test_quitting_with_nothing_running_exits_without_a_dialog() -> None:
    app = NivariaApp(connector=FakeConnector())
    _use_recording_fetch(app)
    pushed: list[type] = []
    real_push_screen = app.push_screen

    def recording_push_screen(screen, *args, **kwargs):
        pushed.append(type(screen))
        return real_push_screen(screen, *args, **kwargs)

    app.push_screen = recording_push_screen  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _connect(pilot, "TEST")
        await pilot.press("ctrl+q")
        await settle(pilot)
        assert app.is_running is False
    assert ClosingDialog not in pushed


async def test_a_call_from_a_thread_after_the_app_exited_is_dropped() -> None:
    """Connecting, role switches and a fetch abandoned by a second quit all
    run on threads that cannot be interrupted, and any of them can finish
    after the app is gone. Textual raised "App is not running" in that
    thread.

    run_async rather than run_test: only a real run clears the loop that
    Textual's own check looks at, as it is after quitting."""
    app = NivariaApp(connector=FakeConnector())

    async def quit_at_once(pilot) -> None:
        await pilot.pause()
        pilot.app.exit()

    await app.run_async(headless=True, size=(120, 40), auto_pilot=quit_at_once)
    called: list[bool] = []
    errors: list[BaseException] = []

    def straggler() -> None:
        try:
            app.call_from_thread(called.append, True)
        except BaseException as error:  # noqa: BLE001 -- the assertion below
            errors.append(error)

    thread = threading.Thread(target=straggler)
    thread.start()
    thread.join(timeout=3.0)
    assert errors == []
    assert called == []


def test_a_cache_update_after_shutdown_does_not_crash_the_app() -> None:
    """Regression: a fetch worker still mid-query when quit is pressed can
    finish (successfully or not) after Textual's own shutdown has already
    popped every screen -- that happens before this app's on_unmount even
    runs. Routing the update by touching self.screen unconditionally then
    raised ScreenStackError from a background thread's call_from_thread,
    surfacing as an unhandled backtrace on the way out instead of a quiet
    exit."""
    app = NivariaApp(connector=FakeConnector())
    assert app.screen_stack == []
    app._post_cache_updated(databases_scope(), True)  # must not raise


async def test_the_cache_is_not_opened_until_an_identity_is_known() -> None:
    """Which file to open depends on the connection's account and role (see
    paths.cache_file), so unlike before, nothing is prepared at bare startup."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.repo is None


async def test_the_cache_is_prepared_once_connected(tmp_path) -> None:
    """The UI reads only the cache, so it has to exist before the browser
    screen can ask it anything -- which is as soon as a pool exists."""
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        screen.query_one("#account", Input).value = "ACME"
        screen.query_one("#user", Input).value = "me"
        screen.submit()
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert app.repo is not None
        assert app.repo._db.path.exists()


async def test_a_cache_that_cannot_be_opened_costs_the_cache_not_the_session(
    tmp_path, monkeypatch
) -> None:
    """Reading a file that never opened raises, which on the footer's tick would
    end the app. Without it, the browser still connects, paints, fetches and
    searches, and says that nothing will be kept."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(paths, "cache_file", lambda *_: blocker / "cache.db")
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        screen.query_one("#account", Input).value = "ACME"
        screen.query_one("#user", Input).value = "me"
        screen.submit()
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)
        assert app.repo.entry(databases_scope()).answered
        assert app.repo.search_corpus(["MYDB"]) == []
        toasts = [toast.message for toast in app._notifications]
    assert any("Cannot open the cache" in toast for toast in toasts)


# ------------------------------------------------------------- main screen


def _repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


async def test_the_main_screen_has_its_two_panes(tmp_path) -> None:
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(MainScreen(_repo(tmp_path)))
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.query_one("#tree")
        assert screen.query_one("#detail")


async def test_the_command_palette_binding_is_hidden_from_the_footer(tmp_path) -> None:
    """The footer's right corner is nivaria's own status indicators, so
    Textual's ctrl+p key must not claim it.

    tcoda's footer needs no setting for that: ctrl+p is an app binding like any
    other and is declared `show=False`, so it is only ever rendered if the app
    asks for it by name in `pinned=`.
    """
    app = NivariaApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(MainScreen(_repo(tmp_path)))
        for _ in range(4):
            await pilot.pause()
        actions = {key.action for key in pilot.app.screen.query(FooterBinding)}
    assert "command_palette" not in actions
