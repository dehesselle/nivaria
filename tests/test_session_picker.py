# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The warehouse and role pickers: same fuzzy-picker pattern as the database
picker (see test_main_screen.py), but, unlike the database picker, actually
rewriting the pool's session."""

from __future__ import annotations

from pathlib import Path

import pytest
from tcoda.chords import ChordMixin
from textual.app import App
from textual.widgets import Input, OptionList

import nivaria.ui.app as app_module
from nivaria import paths
from nivaria.cache.db import Database
from nivaria.cache.repository import Repository
from nivaria.models import databases_scope, roles_scope, warehouses_scope
from nivaria.ui.screens.main import MainScreen
from nivaria.ui.screens.picker import PickerDialog
from nivaria.ui.widgets.status_bar import RoleLabel, WarehouseLabel
from tests.fakes import make_pool
from tests.test_main_screen import RecordingFetch, load, settle


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(ChordMixin, App[None]):
    """Stands in for NivariaApp: MainScreen's role-switch path needs the app
    to repoint it at a different identity's cache (see NivariaApp.switch_repo
    / restart_fetch), so this fake provides the same two methods, backed by a
    real per-identity Repository via the same paths.cache_file every real
    switch goes through -- which is what lets a test prove a role's cache
    survives being switched away from and back to.
    """

    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, fetch, pool) -> None:
        super().__init__()
        self._pool = pool
        self.repo = repo
        self.fetch = fetch

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self.repo, self.fetch, self._pool))

    def restart_fetch(self, pool) -> None:
        self.fetch = RecordingFetch()

    def switch_repo(self, pool) -> None:
        database = Database(paths.cache_file(pool.params.account, pool.params.role))
        database.prepare()
        self.repo = Repository(database)
        self.restart_fetch(pool)


def put_warehouses(repo: Repository, *names: str) -> None:
    load(repo, warehouses_scope(), [{"name": name, "comment": ""} for name in names])


def put_roles(repo: Repository, *names: str) -> None:
    load(repo, roles_scope(), [{"name": name, "comment": ""} for name in names])


async def open_via_chord(pilot, step: str) -> None:
    """Press the chord the way a user would: `,`, Account, then the entry."""
    await pilot.press("comma", "a", step)
    await settle(pilot)


async def test_the_warehouse_chord_opens_a_picker_over_cached_warehouses(repo) -> None:
    put_warehouses(repo, "COMPUTE_WH", "ETL_WH")
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_chord(pilot, "w")
        assert isinstance(app.screen, PickerDialog)


async def test_the_warehouse_picker_shows_size_as_a_hint(repo) -> None:
    load(
        repo,
        warehouses_scope(),
        [{"name": "COMPUTE_WH", "comment": "", "size": "X-Small"}],
    )
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_chord(pilot, "w")
        options = app.screen.query_one("#picker-results", OptionList)
        prompt = options.get_option_at_index(0).prompt
    assert prompt.plain == "COMPUTE_WH  X-Small"


async def test_the_default_warehouse_shows_a_default_hint(repo) -> None:
    load(
        repo,
        warehouses_scope(),
        [
            {"name": "COMPUTE_WH", "comment": "", "size": "X-Small", "is_default": "Y"},
            {"name": "ETL_WH", "comment": "", "size": "Large", "is_default": "N"},
        ],
    )
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_chord(pilot, "w")
        options = app.screen.query_one("#picker-results", OptionList)
        default_prompt = options.get_option_at_index(0).prompt
        other_prompt = options.get_option_at_index(1).prompt
    assert default_prompt.plain == "COMPUTE_WH  X-Small · default"
    assert other_prompt.plain == "ETL_WH  Large"


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
        await open_via_chord(pilot, "w")
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
        await open_via_chord(pilot, "w")
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
    assert all(c.executed[-1] == 'USE WAREHOUSE "ETL_WH"' for c in pool._all)


async def test_picking_a_role_switches_every_pooled_connection(repo) -> None:
    put_roles(repo, "SYSADMIN", "ACCOUNTADMIN")
    pool, _ = make_pool(size=2)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_chord(pilot, "r")
        app.screen.query_one("#picker-query", Input).value = "SYSADMIN"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        for _ in range(20):
            if pool.params.role == "SYSADMIN":
                break
            await pilot.pause()
    assert pool.params.role == "SYSADMIN"
    assert all(c.executed[-1] == 'USE ROLE "SYSADMIN"' for c in pool._all)


async def test_a_successful_role_switch_repoints_the_screen_at_a_new_cache(
    repo,
) -> None:
    """No clear_all, no wipe: a different Repository object entirely, backed
    by that role's own file (see NivariaApp.switch_repo)."""
    put_roles(repo, "SYSADMIN")
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        original_repo = app.screen.repo
        await open_via_chord(pilot, "r")
        app.screen.query_one("#picker-query", Input).value = "SYSADMIN"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        for _ in range(20):
            if pool.params.role == "SYSADMIN":
                break
            await pilot.pause()
        await pilot.pause()
        assert app.screen.repo is not original_repo
        assert app.repo is app.screen.repo
        # Nothing was picked yet under the new identity, so browsing resets --
        # same as a fresh connect, not a database switch.
        assert app.screen.databases == []


async def test_switching_back_to_a_role_reopens_its_own_warm_cache(repo) -> None:
    """The whole point of a file-per-identity cache: an admin alternating
    between two roles should not pay a full refetch every time they switch
    back to one they were already using this session."""
    pool, _ = make_pool(size=1)
    pool.connect_all()

    # Already acting as ROLE_A, with a database cached from before -- as if
    # this session had switched to it earlier.
    pool.set_role("ROLE_A")
    role_a_path = paths.cache_file(pool.params.account, "ROLE_A")
    role_a_database = Database(role_a_path)
    assert role_a_database.prepare()
    role_a_repo = Repository(role_a_database)
    put_roles(role_a_repo, "ROLE_A", "ROLE_B")
    load(role_a_repo, databases_scope(), [{"name": "ROLE_A_DB", "comment": ""}])

    app = Harness(role_a_repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        # Switch away to ROLE_B.
        await open_via_chord(pilot, "r")
        app.screen.query_one("#picker-query", Input).value = "ROLE_B"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        for _ in range(20):
            if pool.params.role == "ROLE_B":
                break
            await pilot.pause()
        assert pool.params.role == "ROLE_B"
        # A brand-new identity's cache starts with nothing in it.
        assert app.repo.databases(databases_scope()).is_empty
        # SHOW ROLES would return the same roles regardless of which one is
        # active, so ROLE_B's own cache offers ROLE_A again too.
        put_roles(app.repo, "ROLE_A", "ROLE_B")

        # Switch back to ROLE_A.
        await open_via_chord(pilot, "r")
        app.screen.query_one("#picker-query", Input).value = "ROLE_A"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        for _ in range(20):
            if pool.params.role == "ROLE_A":
                break
            await pilot.pause()
        await pilot.pause()

    assert pool.params.role == "ROLE_A"
    # The database cached before ever switching away is still there, warm --
    # nothing had to be refetched to see it again.
    assert [d.name for d in app.repo.databases(databases_scope()).rows] == ["ROLE_A_DB"]


async def test_reselecting_the_current_warehouse_does_nothing(repo) -> None:
    put_warehouses(repo, "COMPUTE_WH")
    pool, _ = make_pool(size=1)
    pool.connect_all()
    pool.set_warehouse("COMPUTE_WH")
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_chord(pilot, "w")
        await pilot.press("enter")
        await settle(pilot)
    # The one USE from the setup call above, and no more from reselecting it.
    uses = [s for s in pool._all[0].executed if s.startswith("USE ")]
    assert uses == ['USE WAREHOUSE "COMPUTE_WH"']


# -------------------------------------------------------------------- footer


async def test_the_footer_shows_none_before_anything_is_picked(repo) -> None:
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen._tick()
        await pilot.pause()
        assert app.screen.query_one(WarehouseLabel).warehouse is None
        assert app.screen.query_one(RoleLabel).role is None


async def test_the_footer_reflects_a_completed_switch(repo) -> None:
    put_warehouses(repo, "COMPUTE_WH")
    pool, _ = make_pool(size=1)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await open_via_chord(pilot, "w")
        await pilot.press("enter")
        await settle(pilot)
        for _ in range(20):
            app.screen._tick()
            await pilot.pause()
            if pool.params.warehouse == "COMPUTE_WH":
                break
        warehouse = app.screen.query_one(WarehouseLabel).warehouse
    assert warehouse == "COMPUTE_WH"
