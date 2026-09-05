"""Main screen wiring: scope routing, the database picker, refresh."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Input, Static

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    databases_scope,
    schemas_scope,
)
from snowcone.ui.screens.main import CacheUpdated, MainScreen
from snowcone.ui.screens.picker import PickerScreen
from snowcone.ui.widgets.object_tree import ObjectTree


class RecordingFetch:
    """Stands in for FetchService, recording intent instead of fetching."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []
        self.cancelled: list[str] = []

    def request(self, scope, priority) -> bool:
        self.requests.append((scope.key, int(priority)))
        return True

    def cancel_where(self, predicate) -> list:
        self.cancelled.append("called")
        return []

    @property
    def scopes(self) -> list[str]:
        return [key for key, _ in self.requests]


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, fetch: RecordingFetch) -> None:
        super().__init__()
        self._repo = repo
        self._fetch = fetch

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self._repo, self._fetch))


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


def load(repo: Repository, scope, rows) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


def put_databases(repo: Repository, *names: str) -> None:
    load(repo, databases_scope(), [{"name": name, "comment": ""} for name in names])


def put_schemas(repo: Repository, database: str, *names: str) -> None:
    load(
        repo,
        schemas_scope(database),
        [{"database_name": database, "name": name, "comment": ""} for name in names],
    )


async def settle(pilot, rounds: int = 3) -> None:
    for _ in range(rounds):
        await pilot.pause()
        await asyncio.sleep(0.02)
        await pilot.pause()


# ---------------------------------------------------------------- start-up


async def test_the_database_list_is_requested_on_mount(repo) -> None:
    """Nothing can be browsed until there is a database to browse."""
    fetch = RecordingFetch()
    async with Harness(repo, fetch).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
    assert "databases" in fetch.scopes


async def test_no_database_is_selected_initially(repo) -> None:
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        button = app.screen.query_one("#db-button", Static)
        assert "(none)" in str(button.render())


# ----------------------------------------------------------------- routing


async def test_a_cache_update_reaches_only_the_tree_node_it_names(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("MYDB")
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        node = tree.root.children[0]
        assert node.label.plain == "Schemas (1)"

        put_schemas(repo, "MYDB", "AAA", "BBB")
        screen.post_message(CacheUpdated(schemas_scope("MYDB"), True))
        await pilot.pause()
        assert node.label.plain == "Schemas (2)"


async def test_an_update_for_an_unshown_scope_is_ignored(repo) -> None:
    """Routing, not broadcasting: a scope nothing displays changes nothing."""
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("MYDB")
        await pilot.pause()
        screen.post_message(CacheUpdated(schemas_scope("SOMETHING_ELSE"), True))
        await pilot.pause()
        assert (
            screen.query_one(ObjectTree).root.children[0].label.plain == "Schemas (1)"
        )


async def test_an_objects_request_is_upgraded_to_a_database_prime(repo) -> None:
    """One statement for the whole database beats a round trip per schema."""
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("MYDB")
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        tree.root.children[0].expand()
        await pilot.pause()
        tree.root.children[0].children[0].expand()
        await settle(pilot)
    assert "tables@MYDB" in fetch.scopes
    assert "tables:MYDB.AAA" not in fetch.scopes


async def test_an_interactive_need_outranks_a_visible_one(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("MYDB")
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        tree.root.children[0].expand()
        await pilot.pause()
        tree.root.children[0].children[0].expand()
        await settle(pilot)
    priorities = dict(fetch.requests)
    # Categories become visible without being asked for individually.
    assert priorities["tables@MYDB"] == 1


# ---------------------------------------------------------------- database


async def test_choosing_a_database_updates_the_button_and_the_tree(repo) -> None:
    put_databases(repo, "ALPHA", "BETA")
    put_schemas(repo, "BETA", "S1", "S2")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("BETA")
        await pilot.pause()
        assert "BETA" in str(screen.query_one("#db-button", Static).render())
        assert (
            screen.query_one(ObjectTree).root.children[0].label.plain == "Schemas (2)"
        )


async def test_switching_database_cancels_the_old_ones_work(repo) -> None:
    """A sweep for a database nobody is looking at any more is pure waste."""
    put_databases(repo, "ALPHA", "BETA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("ALPHA")
        await pilot.pause()
        screen.set_database("BETA")
        await pilot.pause()
    assert fetch.cancelled == ["called"]


async def test_reselecting_the_same_database_does_nothing(repo) -> None:
    put_databases(repo, "ALPHA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("ALPHA")
        await pilot.pause()
        screen.set_database("ALPHA")
        await pilot.pause()
    assert fetch.cancelled == []


async def test_ctrl_d_opens_the_picker(repo) -> None:
    put_databases(repo, "ALPHA", "BETA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+d")
        await settle(pilot)
        assert isinstance(app.screen, PickerScreen)


async def test_picking_from_the_dialog_selects_the_database(repo) -> None:
    put_databases(repo, "ALPHA", "BETA")
    put_schemas(repo, "BETA", "S1")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        main = app.screen
        await pilot.press("ctrl+d")
        await settle(pilot)
        app.screen.query_one("#picker-query", Input).value = "BETA"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert main.database == "BETA"


async def test_the_picker_is_not_opened_with_nothing_to_show(repo) -> None:
    """An empty dialog is a dead end; asking for the list is the useful act."""
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+d")
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)
    assert fetch.scopes.count("databases") >= 2


# ----------------------------------------------------------------- refresh


async def test_refresh_targets_the_scope_under_the_cursor(repo) -> None:
    """Surgical, not 'fetch everything again'."""
    put_databases(repo, "MYDB")
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("MYDB")
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        tree.root.children[0].expand()
        await pilot.pause()
        tree.focus()
        tree.move_cursor(tree.root.children[0])
        await pilot.pause()
        fetch.requests.clear()
        screen.action_refresh()
        await pilot.pause()
    assert fetch.scopes == ["schemas:MYDB"]


async def test_refresh_on_an_object_leaf_asks_for_nothing(repo) -> None:
    """A leaf owns no scope of its own; its columns are the detail pane's job."""
    put_databases(repo, "MYDB")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_database("MYDB")
        await pilot.pause()
        fetch.requests.clear()
        screen.action_refresh()
        await pilot.pause()
    assert fetch.scopes == []


# ------------------------------------------------------------------ picker


async def test_the_picker_filters_as_you_type(repo) -> None:
    from textual.widgets import OptionList

    class PickerApp(App[None]):
        CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")
        chosen: object = "unset"

        def on_mount(self) -> None:
            self.push_screen(
                PickerScreen(["ORDERS", "CUSTOMERS", "ORDER_ITEMS"]),
                callback=lambda value: setattr(self, "chosen", value),
            )

    app = PickerApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#picker-results", OptionList)
        assert options.option_count == 3
        app.screen.query_one("#picker-query", Input).value = "ordit"
        await settle(pilot)
        assert options.option_count == 1
        await pilot.press("enter")
        await settle(pilot)
    assert app.chosen == "ORDER_ITEMS"


async def test_escaping_the_picker_chooses_nothing(repo) -> None:
    class PickerApp(App[None]):
        CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")
        chosen: object = "unset"

        def on_mount(self) -> None:
            self.push_screen(
                PickerScreen(["A", "B"]),
                callback=lambda value: setattr(self, "chosen", value),
            )

    app = PickerApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
    assert app.chosen is None
