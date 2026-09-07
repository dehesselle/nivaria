from __future__ import annotations

import asyncio

from textual.widgets import Footer, Input

from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.ui.app import SnowconeApp
from snowcone.ui.screens.connect import ConnectScreen
from snowcone.ui.screens.main import MainScreen
from tests.fakes import FakeConnector


async def settle(pilot, rounds: int = 4) -> None:
    for _ in range(rounds):
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()


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
    """The menu bar's only item today: get back to the connect dialog without
    restarting the app, e.g. to switch accounts mid-session."""
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
        await pilot.press("c")  # "Connect" menu's mnemonic
        await pilot.pause()
        await pilot.press("o")  # "Open..." item's mnemonic
        await settle(pilot)
        assert isinstance(pilot.app.screen, ConnectScreen)


async def test_quitting_the_connect_screen_exits() -> None:
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
    assert app.pool is None


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
    """The footer's right corner is reserved for snowcone's own status
    indicators, so Textual's ctrl+p key must not claim it."""
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(MainScreen(_repo(tmp_path)))
        await pilot.pause()
        footer = pilot.app.screen.query_one(Footer)
        assert footer.show_command_palette is False
