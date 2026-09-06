"""The menu bar: auto-hide, the staged bar/dropdown flow, and mnemonics."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import OptionList, Static

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.ui.screens.main import MainScreen
from snowcone.ui.screens.menu_dropdown import MenuDropdown, MenuItem
from snowcone.ui.screens.menu_select import MenuBarSelectScreen
from snowcone.ui.widgets.menubar import MenuBar
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


async def test_the_bar_is_hidden_until_a_menu_is_opened(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.query_one(MenuBar).display is False


async def test_f9_reveals_the_bar_in_its_bar_only_stage(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        await pilot.press("f9")
        await pilot.pause()
        assert screen.query_one(MenuBar).display is True
        assert isinstance(app.screen, MenuBarSelectScreen)
        assert screen.query_one("#menu-connect").armed is True


async def test_escape_closes_the_bar_only_stage_and_conceals_the_bar_again(
    repo,
) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is screen
        assert screen.query_one(MenuBar).display is False


async def test_enter_drops_the_armed_menu(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, MenuDropdown)
        labels = {
            item.label for item in app.screen._items if isinstance(item, MenuItem)
        }
        assert "Open..." in labels


async def test_the_menus_own_mnemonic_drops_it_directly_from_the_bar_stage(
    repo,
) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("c")  # "Connect" menu's own mnemonic
        await pilot.pause()
        assert isinstance(app.screen, MenuDropdown)


async def test_clicking_the_title_opens_its_dropdown(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        # Auto-hidden by default -- nothing to click until it's shown.
        screen.query_one(MenuBar).auto_hide = False
        await pilot.pause()
        await pilot.click("#menu-connect")
        await pilot.pause()
        assert isinstance(app.screen, MenuDropdown)


async def test_the_connect_menu_lists_the_database_item(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        labels = {
            item.label for item in app.screen._items if isinstance(item, MenuItem)
        }
        assert "Database ..." in labels


async def test_the_open_item_mnemonic_calls_back_into_the_app(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("o")  # "Open..." item's mnemonic
        await pilot.pause()
        assert app.open_connect_calls == 1
        # The dropdown closes and the bar conceals itself again afterwards.
        assert app.screen is screen
        assert screen.query_one(MenuBar).display is False


async def test_disabling_auto_hide_keeps_the_bar_visible_after_the_menu_closes(
    repo,
) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one(MenuBar).auto_hide = False
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.press("o")
        await pilot.pause()
        assert screen.query_one(MenuBar).display is True


async def test_the_bar_shows_one_menu_title(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)):
        assert app.screen.query_one(MenuBar).query_one("#menu-connect")


async def test_the_bar_shows_the_version_right_aligned(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)):
        version_label = app.screen.query_one("#menubar-version", Static)
        assert VERSION in str(version_label.render())


async def test_the_separator_after_open_draws_no_row_of_its_own(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        option_list = app.screen.query_one(OptionList)
        item_count = sum(
            1 for entry in app.screen._items if isinstance(entry, MenuItem)
        )
        assert option_list.option_count == item_count


async def test_the_separator_renders_as_a_rule_under_the_open_item(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        option_list = app.screen.query_one(OptionList)
        open_option = option_list.get_option("open")
        assert open_option._divider is True


async def test_pressing_down_from_open_skips_the_separator_onto_database(
    repo,
) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        option_list = app.screen.query_one(OptionList)
        option_list.highlighted = 0  # "Open..."
        await pilot.press("down")
        await pilot.pause()
        highlighted = option_list.options[option_list.highlighted]
        assert highlighted.id == "database"


async def test_f10_quits_the_app(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f10")
        await pilot.pause()
        assert app._exit is True


async def test_the_quit_item_has_no_mnemonic_and_shows_its_f10_shortcut(
    repo,
) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        quit_item = next(
            entry
            for entry in app.screen._items
            if isinstance(entry, MenuItem) and entry.id == "quit"
        )
        assert quit_item.mnemonic is None
        assert quit_item.shortcut == "F10"


async def test_the_database_item_shows_its_ctrl_d_shortcut(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        database_item = next(
            entry
            for entry in app.screen._items
            if isinstance(entry, MenuItem) and entry.id == "database"
        )
        assert database_item.shortcut == "Ctrl+D"


async def test_the_warehouse_item_shows_its_ctrl_w_shortcut(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        warehouse_item = next(
            entry
            for entry in app.screen._items
            if isinstance(entry, MenuItem) and entry.id == "warehouse"
        )
        assert warehouse_item.shortcut == "Ctrl+W"


async def test_the_role_item_shows_its_ctrl_r_shortcut(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        role_item = next(
            entry
            for entry in app.screen._items
            if isinstance(entry, MenuItem) and entry.id == "role"
        )
        assert role_item.shortcut == "Ctrl+R"


async def test_the_separator_renders_as_a_rule_under_the_prewarm_item(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        option_list = app.screen.query_one(OptionList)
        prewarm_option = option_list.get_option("prewarm")
        assert prewarm_option._divider is True


async def test_selecting_quit_from_the_menu_exits_the_app(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        option_list = app.screen.query_one(OptionList)
        option_list.highlighted = option_list.get_option_index("quit")
        await pilot.press("enter")
        await pilot.pause()
        assert app._exit is True
