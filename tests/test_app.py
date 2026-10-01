from __future__ import annotations

import asyncio

from tcw.widgets import FooterBinding
from textual.widgets import Input

from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import databases_scope, grants_scope, tag_references_scope
from snowcone.ui.app import SnowconeApp
from snowcone.ui.screens.closing import ClosingDialog
from snowcone.ui.screens.connect import ConnectScreen
from snowcone.ui.screens.main import MainScreen
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
    app = SnowconeApp(connector=FakeConnector())
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


async def test_one_dropped_object_failing_several_scopes_toasts_once() -> None:
    """Opening a dropped schema fails its Grants and its Tags together."""
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._report_fetch_failure(grants_scope("MYDB", "GONE"), _GONE, True)
        app._report_fetch_failure(tag_references_scope("MYDB", "GONE"), _GONE, True)
        await pilot.pause()
        assert len(list(app._notifications)) == 1


async def test_the_app_opens_the_connect_screen_first() -> None:
    """Nothing can be browsed before a pool exists, and opening that pool is
    where the MFA prompts happen."""
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, ConnectScreen)


async def test_connecting_hands_over_to_the_browser() -> None:
    """The default pool size is more than one, so this also exercises
    confirming the extra connections on the way to the browser."""
    app = SnowconeApp(connector=FakeConnector())
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


async def test_connect_open_menu_item_reopens_the_connect_dialog() -> None:
    """Get back to the connect dialog without restarting the app, e.g. to
    switch accounts mid-session."""
    app = SnowconeApp(connector=FakeConnector())
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

        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("c")  # "Connection" menu's mnemonic
        await pilot.pause()
        await pilot.press("o")  # "Open..." item's mnemonic
        await settle(pilot)
        assert isinstance(pilot.app.screen, ConnectScreen)


class RecordingFetch:
    """A minimal FetchService stand-in that records when, and against which
    screen state, it was told to stop -- used to prove
    SnowconeApp.open_connect fully quiesces the outgoing identity before
    MainScreen is repointed at a new one, the same discipline
    MainScreen._switch_role already uses for a role switch (see DESIGN.md).
    """

    def __init__(self, app: SnowconeApp) -> None:
        self._app = app
        self.stopped = False
        #: Whether the MainScreen still on the stack (possibly under a modal)
        #: still had `.fetch is self` at the moment stop() ran -- False would
        #: mean the screen had already been repointed at a successor before
        #: this one finished quiescing.
        self.screen_still_pointed_here_when_stopped: bool | None = None

    def request(self, scope, priority) -> bool:
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


def _use_recording_fetch(app: SnowconeApp) -> None:
    """Swap FetchService for RecordingFetch on this instance, so a test can
    observe reconnect ordering/timing with no real worker threads.

    An instance attribute, not a subclass: Textual resolves an inherited
    `CSS_PATH` relative to whichever module actually defines the class, so a
    `SnowconeApp` subclass declared in this test file would otherwise go
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
    await pilot.press("f9")
    await pilot.pause()
    await pilot.press("c")  # "Connection" menu's mnemonic
    await pilot.pause()
    await pilot.press("o")  # "Open..." item's mnemonic
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
    app = SnowconeApp(connector=FakeConnector())
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
    app = SnowconeApp(connector=FakeConnector())
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


async def test_quitting_the_connect_screen_exits() -> None:
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
    assert app.pool is None


def test_a_cache_update_after_shutdown_does_not_crash_the_app() -> None:
    """Regression: a fetch worker still mid-query when quit is pressed can
    finish (successfully or not) after Textual's own shutdown has already
    popped every screen -- that happens before this app's on_unmount even
    runs. Routing the update by touching self.screen unconditionally then
    raised ScreenStackError from a background thread's call_from_thread,
    surfacing as an unhandled backtrace on the way out instead of a quiet
    exit."""
    app = SnowconeApp(connector=FakeConnector())
    assert app.screen_stack == []
    app._post_cache_updated(databases_scope(), True)  # must not raise


async def test_the_cache_is_not_opened_until_an_identity_is_known() -> None:
    """Which file to open depends on the connection's account and role (see
    paths.cache_file), so unlike before, nothing is prepared at bare startup."""
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.repo is None


async def test_the_cache_is_prepared_once_connected(tmp_path) -> None:
    """The UI reads only the cache, so it has to exist before the browser
    screen can ask it anything -- which is as soon as a pool exists."""
    app = SnowconeApp(connector=FakeConnector())
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


# ------------------------------------------------------------- main screen


def _repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


async def test_the_main_screen_has_its_two_panes(tmp_path) -> None:
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(MainScreen(_repo(tmp_path)))
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.query_one("#tree")
        assert screen.query_one("#detail")


async def test_the_command_palette_binding_is_hidden_from_the_footer(tmp_path) -> None:
    """The footer's right corner is snowcone's own status indicators, so
    Textual's ctrl+p key must not claim it.

    tcw's footer needs no setting for that: ctrl+p is an app binding like any
    other and is declared `show=False`, so it is only ever rendered if the app
    asks for it by name in `pinned=`.
    """
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(MainScreen(_repo(tmp_path)))
        for _ in range(4):
            await pilot.pause()
        actions = {key.action for key in pilot.app.screen.query(FooterBinding)}
    assert "command_palette" not in actions
