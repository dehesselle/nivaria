# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""nivaria's chords: the commands under `,`.

Matching keys, holding a chord and laying out the panel belong to `tcoda` and
are tested there. What is nivaria's is *what the chords offer* and what each one
actually does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tcoda.chords import ChordMixin
from tcoda.dialogs import AboutDialog, SettingsDialog
from tcoda.widgets import ChordViewer, FooterBinding
from tcoda.widgets._chord_viewer import ChordViewerEntry
from textual.app import App
from textual.widgets import Input, OptionList, Static, TextArea

import nivaria.ui.app as app_module
from nivaria import __version__
from nivaria.cache.db import Database
from nivaria.cache.repository import Repository
from nivaria.models import databases_scope, roles_scope, warehouses_scope
from nivaria.ui.screens.main import MainScreen
from nivaria.ui.screens.multipicker import MultiPickerDialog
from nivaria.ui.screens.picker import PickerDialog
from tests.fakes import make_pool
from tests.test_main_screen import RecordingFetch, load, settle


class Harness(ChordMixin, App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self._repo = repo
        self._pool, _ = make_pool(size=1)
        self._pool.connect_all()
        self.open_connect_calls = 0

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self._repo, RecordingFetch(), self._pool))

    async def open_connect(self) -> None:
        self.open_connect_calls += 1


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    repository = Repository(database)
    for scope, name in (
        (databases_scope(), "SOME_DB"),
        (warehouses_scope(), "SOME_WH"),
        (roles_scope(), "SOME_ROLE"),
    ):
        load(repository, scope, [{"name": name, "comment": ""}])
    return repository


def entries(app: App[None]) -> list[tuple[str, str]]:
    """What the panel lists, as (key, description)."""
    return [
        (entry.item.key_display, entry.item.description)
        for entry in app.screen.query(ChordViewerEntry)
    ]


# ------------------------------------------------------------------ contents


async def test_the_panel_offers_the_documented_commands_at_the_top(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.query_one(ChordViewer).delay = 0
        await pilot.press("comma")
        await settle(pilot)
        assert entries(app) == [
            ("?", "About"),
            ("a", "+Account"),
            ("s", "Settings"),
        ]


async def test_the_panel_offers_the_documented_commands_under_account(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.query_one(ChordViewer).delay = 0
        await pilot.press("comma", "a")
        await settle(pilot)
        assert entries(app) == [
            ("c", "Connect"),
            ("d", "Database selection"),
            ("r", "Role selection"),
            ("w", "Warehouse selection"),
        ]


# ------------------------------------------------------------------ behaviour


async def test_connect_reopens_the_connect_dialog(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "a", "c")
        await settle(pilot)
        assert app.open_connect_calls == 1


@pytest.mark.parametrize(
    ("step", "offered"),
    [("w", "SOME_WH"), ("r", "SOME_ROLE")],
    ids=["warehouse", "role"],
)
async def test_a_session_chord_opens_the_matching_picker(repo, step, offered) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "a", step)
        await settle(pilot)
        assert isinstance(app.screen, PickerDialog)
        options = app.screen.query_one(OptionList)
        assert offered in str(options.get_option_at_index(0).prompt)


async def test_database_selection_opens_the_multipicker(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "a", "d")
        await settle(pilot)
        assert isinstance(app.screen, MultiPickerDialog)


async def test_settings_opens_the_settings_dialog(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "s")
        await settle(pilot)
        assert isinstance(app.screen, SettingsDialog)


async def test_about_opens_the_about_dialog_with_the_version(repo) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "question_mark")
        await settle(pilot)
        assert isinstance(app.screen, AboutDialog)
        body = app.screen.query_one("#about-body Static", Static)
        assert __version__ in str(body.render())


async def test_clicking_the_footer_hint_starts_a_chord(repo) -> None:
    """The way in for someone using the mouse, now that no menu title can be
    clicked."""
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        hint = next(
            key for key in app.screen.query(FooterBinding) if key.key == "comma"
        )
        await pilot.click(hint)
        await pilot.pause()
        assert app.pending_chord == ("comma",)


# ------------------------------------------------------------------- the keys


async def test_the_tree_has_the_focus_when_the_screen_opens(repo) -> None:
    """By default the first focusable widget gets it, which is in the concealed
    log panel. Its level filter takes printable keys, so `,` would not start a
    chord there."""
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.focused is app.screen.query_one("#tree")


async def test_a_comma_is_still_typed_in_a_dialogs_input(repo) -> None:
    """The chords are the main screen's, and no dialog is it. A dialog's filter
    has to be able to type `,` like any other character."""
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "a", "w")
        await settle(pilot)
        await pilot.press("comma")
        await settle(pilot)
        assert isinstance(app.screen, PickerDialog)
        assert app.pending_chord == ()
        assert app.screen.query_one("#picker-query", Input).value == ","


async def test_a_chord_starts_with_the_read_only_sql_text_focused(repo) -> None:
    """A TextArea that took key presses would keep `,` from ever being a
    chord on a SQL tab. Read-only, it leaves them alone."""
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        text = TextArea("SELECT 1", read_only=True)
        await app.screen.mount(text)
        text.focus()
        await pilot.pause()
        await pilot.press("comma", "s")
        await settle(pilot)
        assert isinstance(app.screen, SettingsDialog)


@pytest.mark.parametrize("key", ["f9", "ctrl+o", "ctrl+d", "ctrl+w", "ctrl+r"])
async def test_the_keys_the_chords_replaced_do_nothing(repo, key) -> None:
    app = Harness(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press(key)
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)
        assert app.open_connect_calls == 0
