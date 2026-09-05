from __future__ import annotations

import asyncio

from textual.widgets import Footer, Input

import snowcone.ui.app as app_module
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


async def test_connecting_hands_over_to_the_browser(monkeypatch, tmp_path) -> None:
    """The default pool size is more than one, so this also exercises
    confirming the extra connections on the way to the browser."""
    monkeypatch.setattr(app_module.paths, "cache_file", lambda: tmp_path / "cache.db")
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


async def test_quitting_the_connect_screen_exits(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_module.paths, "cache_file", lambda: tmp_path / "cache.db")
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
    assert app.pool is None


async def test_the_cache_is_prepared_before_anything_is_fetched(
    monkeypatch, tmp_path
) -> None:
    """The UI reads only the cache, so it has to exist before the first
    screen can ask it anything."""
    path = tmp_path / "cache.db"
    monkeypatch.setattr(app_module.paths, "cache_file", lambda: path)
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.repo is not None
        assert path.exists()


# ------------------------------------------------------------- main screen


async def test_the_main_screen_has_its_three_panes() -> None:
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(MainScreen(app.repo))
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.query_one("#left")
        assert screen.query_one("#tree")
        assert screen.query_one("#context")
        assert screen.query_one("#detail")


async def test_the_command_palette_binding_is_hidden_from_the_footer() -> None:
    """The footer's right corner is reserved for snowcone's own status
    indicators, so Textual's ctrl+p key must not claim it."""
    app = SnowconeApp(connector=FakeConnector())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(MainScreen(app.repo))
        await pilot.pause()
        footer = pilot.app.screen.query_one(Footer)
        assert footer.show_command_palette is False
