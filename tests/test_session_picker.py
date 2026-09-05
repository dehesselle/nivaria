"""The warehouse and role pickers: same fuzzy-picker pattern as the database
picker (see test_main_screen.py), but wired through the Connect menu and,
unlike the database picker, actually rewriting the pool's session."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Input

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import roles_scope, warehouses_scope
from snowcone.ui.screens.main import MainScreen
from snowcone.ui.screens.picker import PickerScreen
from snowcone.ui.widgets.status_bar import StatusFooter
from tests.fakes import make_pool
from tests.test_main_screen import RecordingFetch, load, settle


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, fetch, pool) -> None:
        super().__init__()
        self._repo = repo
        self._fetch = fetch
        self._pool = pool

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self._repo, self._fetch, self._pool))

    async def open_connect(self) -> None:
        """Never exercised here, but connect_menu_items always builds this
        item, so the menu dropdown needs it to resolve."""


def put_warehouses(repo: Repository, *names: str) -> None:
    load(repo, warehouses_scope(), [{"name": name, "comment": ""} for name in names])


def put_roles(repo: Repository, *names: str) -> None:
    load(repo, roles_scope(), [{"name": name, "comment": ""} for name in names])


async def open_via_menu(pilot, mnemonic: str) -> None:
    """Drive the menu bar exactly the way a user would: F9, enter the armed
    Connect menu, then the item's own mnemonic."""
    await pilot.press("f9")
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press(mnemonic)
    await settle(pilot)


# ------------------------------------------------------------------- listing


async def test_the_connect_menu_lists_warehouse_and_role(repo) -> None:
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        labels = {item.label for item in app.screen._items}
        assert "Warehouse ..." in labels
        assert "Role ..." in labels


async def test_the_warehouse_item_opens_a_picker_over_cached_warehouses(repo) -> None:
    put_warehouses(repo, "COMPUTE_WH", "ETL_WH")
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_menu(pilot, "w")
        assert isinstance(app.screen, PickerScreen)


async def test_an_empty_warehouse_cache_requests_a_fetch_instead_of_a_dead_dialog(
    repo,
) -> None:
    """Mirrors the database picker: an empty dialog is a dead end, so asking
    for the list is the useful act instead."""
    fetch = RecordingFetch()
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, fetch, pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_menu(pilot, "w")
        assert isinstance(app.screen, MainScreen)
    assert "warehouses" in fetch.scopes


# ------------------------------------------------------------------ switching


async def test_picking_a_warehouse_switches_every_pooled_connection(repo) -> None:
    put_warehouses(repo, "COMPUTE_WH", "ETL_WH")
    pool, _ = make_pool(size=2)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_menu(pilot, "w")
        app.screen.query_one("#picker-query", Input).value = "ETL"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        # The switch runs in a background thread; give it a moment to land.
        for _ in range(20):
            if pool.params.warehouse == "ETL_WH":
                break
            await pilot.pause()
    assert pool.params.warehouse == "ETL_WH"
    assert all(c.executed == ['USE WAREHOUSE "ETL_WH"'] for c in pool._all)


async def test_picking_a_role_switches_every_pooled_connection(repo) -> None:
    put_roles(repo, "SYSADMIN", "ACCOUNTADMIN")
    pool, _ = make_pool(size=2)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_menu(pilot, "r")
        app.screen.query_one("#picker-query", Input).value = "SYSADMIN"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        for _ in range(20):
            if pool.params.role == "SYSADMIN":
                break
            await pilot.pause()
    assert pool.params.role == "SYSADMIN"
    assert all(c.executed == ['USE ROLE "SYSADMIN"'] for c in pool._all)


async def test_reselecting_the_current_warehouse_does_nothing(repo) -> None:
    put_warehouses(repo, "COMPUTE_WH")
    pool, _ = make_pool(size=1)
    pool.connect_all()
    pool.set_warehouse("COMPUTE_WH")
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_menu(pilot, "w")
        await pilot.press("enter")
        await settle(pilot)
    # The one USE from the setup call above, and no more from reselecting it.
    assert pool._all[0].executed == ['USE WAREHOUSE "COMPUTE_WH"']


# -------------------------------------------------------------------- footer


async def test_the_footer_shows_none_before_anything_is_picked(repo) -> None:
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen._tick()
        await pilot.pause()
        footer = app.screen.query_one(StatusFooter)
        assert footer.warehouse is None
        assert footer.role is None


async def test_the_footer_reflects_a_completed_switch(repo) -> None:
    put_warehouses(repo, "COMPUTE_WH")
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_menu(pilot, "w")
        await pilot.press("enter")
        await settle(pilot)
        for _ in range(20):
            app.screen._tick()
            await pilot.pause()
            if pool.params.warehouse == "COMPUTE_WH":
                break
        footer = app.screen.query_one(StatusFooter)
    assert footer.warehouse == "COMPUTE_WH"
