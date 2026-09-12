"""snowcone's Connect menu.

The bar itself -- auto-hide, the staged bar/dropdown flow, mnemonics,
separators, the F9 binding -- belongs to `tcw` and is tested there. What is
snowcone's is *what the menu offers* and what each entry actually does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tcw.dialogs import SettingsDialog
from tcw.widgets import Menu, MenuBar, MenuItem
from textual.app import App
from textual.widgets import OptionList, Static

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.ui.screens.main import MainScreen
from snowcone.version import VERSION


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self._repo = repo
        self.open_connect_calls = 0

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self._repo))

    async def open_connect(self) -> None:
        self.open_connect_calls += 1


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


# MainScreen (which owns the MenuBar) stays mounted underneath a pushed modal,
# so a reference grabbed once via `app.screen` right after mount stays valid
# for the rest of the test -- unlike `app.query_one`, which only ever searches
# the app's default screen and cannot see a widget mounted on a screen that
# was explicitly pushed.


async def settle(pilot, rounds: int = 3) -> None:
    for _ in range(rounds):
        await pilot.pause()
        await asyncio.sleep(0.02)


async def open_menu(pilot) -> None:
    """F9, then enter the armed Connect menu."""
    await pilot.press("f9")
    await settle(pilot)
    await pilot.press("enter")
    await settle(pilot)


# ------------------------------------------------------------------ contents


async def test_there_is_one_menu_called_connect(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        titles = [menu.title_text for menu in app.screen.query(Menu)]
        assert titles == ["Connect"]


async def test_the_connect_menu_offers_the_documented_entries(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        labels = [item.label for item in app.screen.query(MenuItem)]
        assert labels == [
            "Open...",
            "Databases ...",
            "Warehouse ...",
            "Role ...",
            "Pre-warm cache",
            "Settings ...",
            "Quit",
        ]


async def test_the_hidden_key_bindings_are_advertised_as_shortcuts(repo) -> None:
    """Ctrl+D/W/R and F10 are `show=False` in the footer, so the menu's
    shortcut column is the only place they are advertised."""
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        shortcuts = {item.id: item.shortcut for item in app.screen.query(MenuItem)}
        assert shortcuts["database"] == "Ctrl+D"
        assert shortcuts["warehouse"] == "Ctrl+W"
        assert shortcuts["role"] == "Ctrl+R"
        assert shortcuts["quit"] == "F10"


async def test_the_separators_group_the_entries(repo) -> None:
    """Three dividers: connect, then session/cache actions, then settings,
    then quit."""
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_menu(pilot)
        option_list = app.screen.query_one(OptionList)
        assert [option.id for option in option_list.options] == [
            "open",
            "database",
            "warehouse",
            "role",
            "prewarm",
            "settings",
            "quit",
        ]


async def test_the_bar_lays_out_as_a_title_plus_the_snowcone_version(repo) -> None:
    """Geometry, not just content: a menu id is a CSS id, so a clash with a
    rule in app.tcss silently resizes the title and the bar turns into a row
    of scrollbar blocks. `#connect` was exactly that clash once.
    """
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        bar = app.screen.query_one(MenuBar)
        bar.auto_hide = False
        # Reappearing changes layout (the rest of the screen gives back its
        # row). One pause only requests the refresh; the compositor doesn't
        # settle regions until the pump after that, so a single pause here
        # sometimes still finds title's region at (0, 0) -- flaky.
        await pilot.pause()
        await pilot.pause()
        title = app.screen.query_one(Menu)
        # "Connect" plus the 2-cell padding either side, on the bar's one row.
        assert (title.region.width, title.region.height) == (11, 1)
        trailing = app.screen.query_one("#menubar-trailing", Static)
        assert trailing.region.x == title.region.right
        assert trailing.region.right == 120
        assert trailing.region.height == 1
        assert VERSION in str(trailing.render())


# ------------------------------------------------------------------ behaviour


async def test_open_reopens_the_connect_dialog(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_menu(pilot)
        await pilot.press("o")
        await settle(pilot)
        assert app.open_connect_calls == 1


async def test_settings_opens_the_settings_dialog(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_menu(pilot)
        await pilot.press("s")
        await settle(pilot)
        assert isinstance(app.screen, SettingsDialog)


async def test_prewarm_without_a_database_says_so_instead_of_priming(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_menu(pilot)
        await pilot.press("p")
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)
        assert any(
            "Select a database" in notification.message
            for notification in app._notifications
        )


async def test_quit_exits_the_app(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_menu(pilot)
        await pilot.press("q")
        await settle(pilot)
    assert app.is_running is False


async def test_f10_quits_too(repo) -> None:
    """The same action as the menu's Quit entry, kept as a binding for the
    people who never open the menu."""
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f10")
        await settle(pilot)
    assert app.is_running is False
