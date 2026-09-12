"""Main screen wiring: scope routing, the database picker, refresh."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Input

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    ObjectType,
    Scope,
    ScopeType,
    columns_scope,
    databases_scope,
    objects_scope,
    schemas_scope,
)
from snowcone.profiles import ProfileStore
from snowcone.ui.messages import ScopeNeeded
from snowcone.ui.screens.main import CacheUpdated, MainScreen
from snowcone.ui.screens.multipicker import MultiPickerDialog
from snowcone.ui.screens.picker import PickerDialog
from snowcone.ui.views.schema_view import DatabaseView, SchemaView
from snowcone.ui.widgets.object_tree import ObjectTree
from tests.fakes import make_pool


class RecordingFetch:
    """Stands in for FetchService, recording intent instead of fetching."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []
        self.cancelled: list[str] = []
        self.busy: set[str] = set()

    def request(self, scope, priority) -> bool:
        self.requests.append((scope.key, int(priority)))
        return True

    def cancel_where(self, predicate) -> list:
        self.cancelled.append("called")
        return []

    def is_busy(self, scope) -> bool:
        return scope.key in self.busy

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 2.0) -> None:
        pass

    @property
    def scopes(self) -> list[str]:
        return [key for key, _ in self.requests]


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(
        self,
        repo: Repository,
        fetch: RecordingFetch,
        pool=None,
        profiles: ProfileStore | None = None,
    ) -> None:
        super().__init__()
        self._repo = repo
        self._fetch = fetch
        self._pool = pool
        self._profiles = profiles

    def on_mount(self) -> None:
        self.push_screen(
            MainScreen(self._repo, self._fetch, self._pool, self._profiles)
        )


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


async def test_warehouses_and_roles_are_also_requested_on_mount(repo) -> None:
    """Regression: fetching them lazily on the picker's first open meant the
    dialog had nothing cached yet, so opening it just re-requested the list
    and closed again -- a second, separate open was needed to see it."""
    fetch = RecordingFetch()
    async with Harness(repo, fetch).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
    assert "warehouses" in fetch.scopes
    assert "roles" in fetch.scopes


async def test_no_database_is_selected_initially(repo) -> None:
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.screen.databases == []


# ----------------------------------------------------------------- routing


async def test_a_cache_update_reaches_only_the_tree_node_it_names(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        node = tree.root.children[0]
        assert node.label.plain == "MYDB (1)"

        put_schemas(repo, "MYDB", "AAA", "BBB")
        screen.post_message(CacheUpdated(schemas_scope("MYDB"), True))
        await pilot.pause()
        assert node.label.plain == "MYDB (2)"


async def test_an_update_for_an_unshown_scope_is_ignored(repo) -> None:
    """Routing, not broadcasting: a scope nothing displays changes nothing."""
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        screen.post_message(CacheUpdated(schemas_scope("SOMETHING_ELSE"), True))
        await pilot.pause()
        assert screen.query_one(ObjectTree).root.children[0].label.plain == "MYDB (1)"


async def test_an_objects_request_is_upgraded_to_a_database_prime(repo) -> None:
    """One statement for the whole database beats a round trip per schema."""
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        tree.root.children[0].expand()
        await pilot.pause()
        tree.root.children[0].children[0].expand()
        await settle(pilot)
    assert "tables@MYDB" in fetch.scopes
    assert "tables:MYDB.AAA" not in fetch.scopes


async def test_only_an_objects_scope_is_upgraded_to_a_prime(repo) -> None:
    """The upgrade keys off ScopeType.OBJECTS, not off "has an object type
    and a database". A SELECT-backed scope that happened to carry both would
    otherwise be silently redirected into fetching SHOW TABLES instead of
    the statement it actually needs."""
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        misshapen = Scope(
            ScopeType.TABLE_STATS,
            database="MYDB",
            schema="AAA",
            object_type=ObjectType.TABLE,
        )
        screen.on_scope_needed(ScopeNeeded(misshapen, interactive=True))
        await settle(pilot)
    assert misshapen.key in fetch.scopes
    assert "tables@MYDB" not in fetch.scopes


async def test_an_interactive_need_outranks_a_visible_one(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
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


async def test_choosing_a_database_updates_the_tree_contents(repo) -> None:
    put_databases(repo, "ALPHA", "BETA")
    put_schemas(repo, "BETA", "S1", "S2")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["BETA"])
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        assert tree.root.children[0].label.plain == "BETA (2)"


async def test_multiple_databases_can_be_selected_at_once(repo) -> None:
    put_databases(repo, "ALPHA", "BETA")
    put_schemas(repo, "ALPHA", "S1")
    put_schemas(repo, "BETA", "S2")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["ALPHA", "BETA"])
        await pilot.pause()
        assert screen.databases == ["ALPHA", "BETA"]
        tree = screen.query_one(ObjectTree)
        assert [child.data.key for child in tree.root.children] == ["ALPHA", "BETA"]


async def test_switching_database_cancels_only_the_removed_ones_work(repo) -> None:
    """A sweep for a database nobody is looking at any more is pure waste --
    but a database that stays selected must not lose its queued work."""
    put_databases(repo, "ALPHA", "BETA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["ALPHA", "BETA"])
        await pilot.pause()
        screen.set_databases(["BETA"])
        await pilot.pause()
    assert fetch.cancelled == ["called"]


async def test_reselecting_the_same_database_does_nothing(repo) -> None:
    put_databases(repo, "ALPHA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["ALPHA"])
        await pilot.pause()
        screen.set_databases(["ALPHA"])
        await pilot.pause()
    assert fetch.cancelled == []


async def test_ctrl_d_opens_the_multipicker(repo) -> None:
    put_databases(repo, "ALPHA", "BETA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+d")
        await settle(pilot)
        assert isinstance(app.screen, MultiPickerDialog)


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
        await pilot.press("down")  # from the query box into the results list
        await pilot.press("space")  # toggles the highlighted row (BETA)
        await settle(pilot)
        await pilot.press("enter")  # confirms -- intercepted, not a toggle
        await settle(pilot)
        assert main.databases == ["BETA"]


async def test_multiple_databases_can_be_picked_at_once(repo) -> None:
    put_databases(repo, "ALPHA", "BETA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        main = app.screen
        await pilot.press("ctrl+d")
        await settle(pilot)
        await pilot.click("#multipicker-all")
        await settle(pilot)
        await pilot.click("#multipicker-ok")
        await settle(pilot)
        assert main.databases == ["ALPHA", "BETA"]


async def test_picking_databases_persists_them_to_the_profile(repo, tmp_path) -> None:
    put_databases(repo, "ALPHA", "BETA")
    fetch = RecordingFetch()
    pool, _ = make_pool(size=1)
    pool.connect_all()
    store = ProfileStore(tmp_path / "recent.json")
    app = Harness(repo, fetch, pool=pool, profiles=store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+d")
        await settle(pilot)
        await pilot.click("#multipicker-all")
        await settle(pilot)
        await pilot.click("#multipicker-ok")
        await settle(pilot)
    assert store.load()[0].databases == ("ALPHA", "BETA")


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
        screen.set_databases(["MYDB"])
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        tree.root.children[0].expand()
        await pilot.pause()
        tree.focus()
        tree.move_cursor(tree.root.children[0])
        # Highlighting the database node now also mounts its Grants/Tags
        # view (DatabaseSelected -> show_database), which requests its own
        # scopes asynchronously -- settle() lets that finish before the
        # requests list is cleared, so only action_refresh's own request
        # below is captured.
        await settle(pilot)
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
        screen.set_databases(["MYDB"])
        # settle(), not a single pause: adding the database node posts its own
        # ScopeNeeded for schemas:MYDB, and one pump cycle is not guaranteed to
        # deliver it. Clearing before it lands made this intermittently see
        # that request as though action_refresh had made it.
        await settle(pilot)
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
                PickerDialog(["ORDERS", "CUSTOMERS", "ORDER_ITEMS"]),
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


async def test_a_candidate_with_a_hint_shows_it_dimmed_next_to_the_name(repo) -> None:
    from textual.widgets import OptionList

    class PickerApp(App[None]):
        CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

        def on_mount(self) -> None:
            self.push_screen(
                PickerDialog(
                    ["COMPUTE_WH", "ETL_WH"],
                    hints={"COMPUTE_WH": ["X-Small"]},
                )
            )

    app = PickerApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#picker-results", OptionList)
        with_hint = options.get_option_at_index(0).prompt
        without_hint = options.get_option_at_index(1).prompt
    assert with_hint.plain == "COMPUTE_WH  X-Small"
    assert without_hint.plain == "ETL_WH"
    assert any(span.style == "dim" for span in with_hint.spans)


async def test_multiple_hints_on_one_candidate_are_joined_by_a_middle_dot(repo) -> None:
    from textual.widgets import OptionList

    class PickerApp(App[None]):
        CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

        def on_mount(self) -> None:
            self.push_screen(
                PickerDialog(
                    ["COMPUTE_WH"],
                    hints={"COMPUTE_WH": ["X-Small", "default"]},
                )
            )

    app = PickerApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#picker-results", OptionList)
        prompt = options.get_option_at_index(0).prompt
    assert prompt.plain == "COMPUTE_WH  X-Small · default"


async def test_a_hinted_candidate_that_is_also_current_shows_both(repo) -> None:
    """Hints and `(current)` are one composed annotation, not two independent
    appends -- this is what would break if they ever drifted back apart. The
    `(current)` marker is joined with a plain gap, not the hint separator: its
    parentheses already set it apart, and it is never a list to separate."""
    from textual.widgets import OptionList

    class PickerApp(App[None]):
        CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

        def on_mount(self) -> None:
            self.push_screen(
                PickerDialog(
                    ["COMPUTE_WH", "ETL_WH"],
                    current="COMPUTE_WH",
                    hints={"COMPUTE_WH": ["X-Small"]},
                )
            )

    app = PickerApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#picker-results", OptionList)
        prompt = options.get_option_at_index(0).prompt
    assert prompt.plain == "COMPUTE_WH  X-Small  (current)"


async def test_escaping_the_picker_chooses_nothing(repo) -> None:
    class PickerApp(App[None]):
        CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")
        chosen: object = "unset"

        def on_mount(self) -> None:
            self.push_screen(
                PickerDialog(["A", "B"]),
                callback=lambda value: setattr(self, "chosen", value),
            )

    app = PickerApp()
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
    assert app.chosen is None


# ------------------------------------------------------------- detail pane


def put_tables(repo: Repository, database: str, schema: str, *names: str) -> None:
    load(
        repo,
        objects_scope(ObjectType.TABLE, database, schema),
        [
            {
                "database_name": database,
                "schema_name": schema,
                "name": name,
                "kind": "TABLE",
                "comment": "",
                "rows": 10,
                "bytes": 1024,
            }
            for name in names
        ],
    )


async def drill_to_tables(app, pilot, screen):
    tree = screen.query_one(ObjectTree)
    tree.root.children[0].expand()
    await pilot.pause()
    schema = tree.root.children[0].children[0]
    schema.expand()
    await pilot.pause()
    tables = schema.children[0]
    tables.expand()
    await pilot.pause()
    return tree, tables


async def test_selecting_a_table_fills_the_detail_pane(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        tree, tables = await drill_to_tables(app, pilot, screen)
        tree.move_cursor(tables.children[0])
        await settle(pilot)
        assert screen.detail.ref is not None
        assert screen.detail.ref.fqn == "MYDB.AAA.ORDERS"


async def test_switching_database_clears_the_detail_pane(repo) -> None:
    """What was on screen belongs to a database nobody is looking at now.

    The pane does not stay empty, though: the rebuilt tree's cursor lands on
    the newly selected database's own top-level node, which -- now that a
    database node announces itself on highlight (DatabaseSelected) -- opens
    that database's own Grants/Tags view. That is fresh content belonging to
    "OTHER", not the stale ORDERS table view left over from "MYDB", which is
    what this test actually protects."""
    put_databases(repo, "MYDB", "OTHER")
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        tree, tables = await drill_to_tables(app, pilot, screen)
        tree.move_cursor(tables.children[0])
        await settle(pilot)
        assert screen.detail.ref is not None
        assert screen.detail.ref.fqn == "MYDB.AAA.ORDERS"

        screen.set_databases(["OTHER"])
        await settle(pilot)
        assert screen.detail.ref is None
        assert isinstance(screen.detail.view, DatabaseView)


async def test_the_sweep_covers_the_detail_pane_too(repo) -> None:
    """The pane's columns scope is on screen, so it is exactly the sort of
    thing the sweep exists to keep fresh."""
    from snowcone.settings import CACHE_TTL, config

    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    load(
        repo,
        columns_scope("MYDB", "AAA", "ORDERS"),
        [
            {
                "TABLE_CATALOG": "MYDB",
                "TABLE_SCHEMA": "AAA",
                "TABLE_NAME": "ORDERS",
                "COLUMN_NAME": "ID",
                "ORDINAL_POSITION": 1,
                "DATA_TYPE": "NUMBER",
                "IS_NULLABLE": "YES",
            }
        ],
    )
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.set_databases(["MYDB"])
            await pilot.pause()
            tree, tables = await drill_to_tables(app, pilot, screen)
            tree.move_cursor(tables.children[0])
            await settle(pilot)

            with repo._db.connect() as connection:
                connection.execute(
                    "UPDATE cache_entry SET fetched_at = fetched_at - 999 "
                    "WHERE scope = ?",
                    (columns_scope("MYDB", "AAA", "ORDERS").key,),
                )
            fetch.requests.clear()
            screen._sweep()
            await pilot.pause()
        assert "columns:MYDB.AAA.ORDERS" in fetch.scopes
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


# ---------------------------------------------------------------- searching


async def test_slash_opens_the_search_modal(repo) -> None:
    from snowcone.ui.screens.search import SearchDialog

    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.query_one(ObjectTree).focus()
        await pilot.pause()
        await pilot.press("slash")
        await settle(pilot)
        assert isinstance(app.screen, SearchDialog)
        assert app.screen.query_one("#search-query", Input).has_focus


async def test_a_search_hit_reveals_in_the_tree_and_fills_the_detail(repo) -> None:
    """A hit has to leave you somewhere you can keep browsing from, not a
    detail pane pointing at an object the tree has never heard of."""
    put_schemas(repo, "MYDB", "AAA", "BBB")
    put_tables(repo, "MYDB", "BBB", "STG_CUSTOMER_ORDERS")
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await settle(pilot)

        await pilot.press("slash")
        await settle(pilot)
        app.screen.query_one("#search-query", Input).value = "stgord"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)

        assert screen.detail.ref.fqn == "MYDB.BBB.STG_CUSTOMER_ORDERS"
        tree = screen.query_one(ObjectTree)
        assert tree.cursor_node is not None
        assert tree.cursor_node.data.key == "STG_CUSTOMER_ORDERS"


async def test_search_is_scoped_to_every_selected_database(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "STG_MYDB_ORDERS")
    put_schemas(repo, "OTHERDB", "BBB")
    put_tables(repo, "OTHERDB", "BBB", "STG_OTHERDB_ORDERS")
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB", "OTHERDB"])
        await settle(pilot)

        await pilot.press("slash")
        await settle(pilot)
        app.screen.query_one("#search-query", Input).value = "stgorders"
        await settle(pilot)
        from textual.widgets import OptionList

        results = app.screen.query_one("#search-results", OptionList)
        shown = {
            results.get_option_at_index(i).prompt.plain
            for i in range(results.option_count)
        }
    assert any("MYDB.AAA.STG_MYDB_ORDERS" in text for text in shown)
    assert any("OTHERDB.BBB.STG_OTHERDB_ORDERS" in text for text in shown)


async def test_a_schema_search_hit_reveals_expands_and_opens_its_view(repo) -> None:
    """A schema hit reveals and expands its node in the tree, same as
    always -- but since that moves the cursor onto the schema node, and a
    schema node now posts SchemaSelected on highlight, choosing a schema hit
    also opens its Grants/Tags view, not just navigating the tree the way it
    used to (see SchemaRef and DESIGN.md's "Choosing a hit")."""
    put_schemas(repo, "MYDB", "AAA", "STAGING")
    put_tables(repo, "MYDB", "STAGING", "STG_CUSTOMER_ORDERS")
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await settle(pilot)

        await pilot.press("slash")
        await settle(pilot)
        app.screen.query_one("#search-query", Input).value = "staging"
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)

        tree = screen.query_one(ObjectTree)
        assert tree.cursor_node is not None
        assert tree.cursor_node.data.key == "STAGING"
        assert tree.cursor_node.is_expanded
        assert screen.detail.ref is None
        assert isinstance(screen.detail.view, SchemaView)


async def test_escape_closes_the_search_modal_without_choosing(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await settle(pilot)

        await pilot.press("slash")
        await settle(pilot)
        app.screen.query_one("#search-query", Input).value = "ord"
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)

        assert app.screen is screen
        assert screen.detail.ref is None
