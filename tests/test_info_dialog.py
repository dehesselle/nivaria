# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The Info dialog: `, a i` shows the session's names in full."""

from __future__ import annotations

from pathlib import Path

import pytest
from tcoda.chords import ChordMixin
from textual.app import App
from textual.geometry import Region
from textual.widgets import Button

import nivaria.ui.app as app_module
from nivaria.cache.db import Database
from nivaria.cache.repository import Repository
from nivaria.snowflake.pool import ConnectionPool, ConnectParams
from nivaria.ui.screens.info import InfoDialog
from nivaria.ui.screens.main import MainScreen
from tests.fakes import FakeConnector
from tests.test_main_screen import RecordingFetch, settle


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(ChordMixin, App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, pool: ConnectionPool | None) -> None:
        super().__init__()
        self._repo = repo
        self._pool = pool

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self._repo, RecordingFetch(), self._pool))


def session_pool(
    account: str = "MYORG-MYACCT",
    role: str | None = "SYSADMIN",
    warehouse: str | None = "COMPUTE_WH",
) -> ConnectionPool:
    pool = ConnectionPool(
        ConnectParams(account=account, user="u", role=role, warehouse=warehouse),
        1,
        connector=FakeConnector(),
    )
    pool.connect_all()
    return pool


def shown_values(dialog: InfoDialog) -> list[str]:
    """Each value as really drawn, with any wrapped lines joined back up."""
    values = []
    for widget in dialog.query(".info-value"):
        size = widget.outer_size
        lines = widget.render_lines(Region(0, 0, size.width, size.height))
        values.append("".join(line.text.rstrip() for line in lines))
    return values


async def open_info(pilot) -> None:
    await pilot.pause()
    await pilot.press("comma", "a", "i")
    await settle(pilot)


async def test_the_chord_opens_the_dialog_with_all_four_values(repo) -> None:
    app = Harness(repo, session_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_info(pilot)
        assert isinstance(app.screen, InfoDialog)
        assert shown_values(app.screen) == ["MYACCT", "SYSADMIN", "COMPUTE_WH", "No"]


async def test_privatelink_reads_yes_for_a_privatelink_identifier(repo) -> None:
    app = Harness(repo, session_pool("MYORG-MYACCT.privatelink"))
    async with app.run_test(size=(120, 40)) as pilot:
        await open_info(pilot)
        assert shown_values(app.screen) == ["MYACCT", "SYSADMIN", "COMPUTE_WH", "Yes"]


async def test_a_session_without_a_warehouse_reads_none(repo) -> None:
    app = Harness(repo, session_pool(warehouse=None))
    async with app.run_test(size=(120, 40)) as pilot:
        await open_info(pilot)
        assert shown_values(app.screen)[2] == "none"


async def test_a_long_name_shows_in_full_even_on_a_small_terminal(repo) -> None:
    """Showing what the borders cut off is the reason this dialog exists."""
    role = "A_VERY_LONG_PRODUCTION_ROLE_NAME_THAT_NEEDS_MORE_THAN_ONE_LINE_TO_FIT_RW"
    app = Harness(repo, session_pool(role=role))
    async with app.run_test(size=(80, 24)) as pilot:
        await open_info(pilot)
        assert shown_values(app.screen)[1] == role


async def test_a_name_with_a_bracket_shows_literally(repo) -> None:
    app = Harness(repo, session_pool(role="[bold]ROLE"))
    async with app.run_test(size=(120, 40)) as pilot:
        await open_info(pilot)
        assert shown_values(app.screen)[1] == "[bold]ROLE"


async def test_ok_is_the_only_button(repo) -> None:
    app = Harness(repo, session_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_info(pilot)
        assert [str(b.label) for b in app.screen.query(Button)] == ["Ok"]


@pytest.mark.parametrize("key", ["enter", "escape"])
async def test_a_key_closes_the_dialog(repo, key: str) -> None:
    app = Harness(repo, session_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_info(pilot)
        await pilot.press(key)
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)


async def test_clicking_ok_closes_the_dialog(repo) -> None:
    app = Harness(repo, session_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_info(pilot)
        await pilot.click("#info-ok")
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)


async def test_the_chord_does_nothing_without_a_pool(repo) -> None:
    app = Harness(repo, None)
    async with app.run_test(size=(120, 40)) as pilot:
        await open_info(pilot)
        assert isinstance(app.screen, MainScreen)
