# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Main screen wiring: scope routing, the database picker, refresh."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tcoda.chords import ChordMixin
from textual.app import App
from textual.widgets import Input

import nivaria.ui.app as app_module
from nivaria.cache.db import Database
from nivaria.cache.repository import Repository
from nivaria.models import (
    ObjectRef,
    ObjectType,
    Scope,
    ScopeType,
    columns_scope,
    databases_scope,
    objects_scope,
    pipe_activity_scope,
    prime_scope,
    schemas_scope,
    task_activity_history_scope,
    task_activity_scope,
    task_history_scope,
)
from nivaria.profiles import Profile, ProfileStore
from nivaria.service.fetch import FetchService
from nivaria.service.jobs import Priority
from nivaria.settings import CACHE_TTL, CURSOR_SETTLE_MS, SNOWSIGHT_URL, config
from nivaria.snowflake.pool import AccountNames, ConnectionPool, ConnectParams
from nivaria.ui.messages import CategorySelected, ObjectSelected, ScopeNeeded
from nivaria.ui.screens.main import CacheUpdated, MainScreen
from nivaria.ui.screens.multipicker import MultiPickerDialog
from nivaria.ui.screens.picker import PickerDialog
from nivaria.ui.views.schema_view import DatabaseView, SchemaView
from nivaria.ui.widgets.object_tree import TREE_TYPES, ObjectTree
from tests.fakes import FakeConnector, FakeGateway, make_pool
from tests.views.harness import put_pipe, put_stage, put_table, put_task, put_view


class RecordingFetch:
    """Stands in for FetchService, recording intent instead of fetching."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []
        self.cancelled: list[str] = []
        self.predicates: list = []
        self.busy: set[str] = set()
        #: (scope key, reason) of every request, for the journal's tests.
        self.reasons: list[tuple[str, str]] = []

    def request(self, scope, priority, *, reason, detail="") -> bool:
        self.requests.append((scope.key, int(priority)))
        self.reasons.append((scope.key, str(reason)))
        # Queued work stays busy until a test lands it, as a real queue's
        # does: the countdown leaves busy scopes out, and a request that
        # never counted as busy would read as overdue on every tick.
        self.busy.add(scope.key)
        return True

    def cancel_where(self, predicate, why="cancelled") -> list:
        self.cancelled.append("called")
        self.predicates.append(predicate)
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


class Harness(ChordMixin, App[None]):
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
    repo.write_page(scope, generation, rows)
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


# ------------------------------------------------------------- startup prime

STARTUP_PRIMES = {
    f"{object_type.plural}@MYDB"
    for object_type in (
        ObjectType.TABLE,
        ObjectType.VIEW,
        ObjectType.PIPE,
        ObjectType.STAGE,
        ObjectType.TASK,
    )
}


async def test_a_selected_databases_objects_are_primed_once_its_schemas_land(
    repo,
) -> None:
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await settle(pilot)
        assert not STARTUP_PRIMES & set(fetch.scopes)

        put_schemas(repo, "MYDB", "AAA")
        screen.post_message(CacheUpdated(schemas_scope("MYDB"), True))
        await settle(pilot)
    assert STARTUP_PRIMES <= set(fetch.scopes)


async def test_a_startup_prime_waits_for_the_last_page_of_the_schema_list(repo) -> None:
    """A prime completes only the schemas known when it runs, so a partial
    list would leave the rest reading as never fetched."""
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        put_schemas(repo, "MYDB", "AAA")
        screen.post_message(CacheUpdated(schemas_scope("MYDB"), False))
        await settle(pilot)
    assert not STARTUP_PRIMES & set(fetch.scopes)


async def test_tables_are_searchable_without_expanding_a_schema(tmp_path) -> None:
    """The whole path, through a real FetchService and the corpus search reads."""
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    repo = Repository(database)
    gateway = FakeGateway(
        schemas={"MYDB": ["AAA"]},
        objects={("MYDB", "AAA", ObjectType.TABLE): ["ORDERS"]},
    )
    app = Harness(repo, None)
    fetch = FetchService(
        gateway,
        repo,
        notify=lambda scope, complete, priority: app.screen.post_message(
            CacheUpdated(scope, complete)
        ),
    )
    app._fetch = fetch
    fetch.start()
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen.set_databases(["MYDB"])
            for _ in range(100):
                await settle(pilot, rounds=1)
                if repo.search_corpus(["MYDB"]):
                    break
    finally:
        fetch.stop()
    assert [text for text, _ in repo.search_corpus(["MYDB"])] == ["AAA.ORDERS"]


async def test_a_task_with_no_warehouse_sends_its_failing_statements_once(repo) -> None:
    """The whole path, through a real FetchService: each failure repaints the
    view, and on a live account that kept both statements running, at about
    one login a second, until the cursor moved away. Cache age 0 turns the sweep
    off: it can legitimately get in first on a tick, and the view's own first
    request then retries."""
    config.set(CACHE_TTL, 0)
    ref = put_task(repo, "NIGHTLY")
    gateway = FakeGateway()
    gateway.raise_on_task_history = True
    gateway.raise_on_task_activity_history = True
    app = Harness(repo, None)
    fetch = FetchService(
        gateway,
        repo,
        notify=lambda scope, complete, priority: app.screen.post_message(
            CacheUpdated(scope, complete)
        ),
    )
    app._fetch = fetch
    fetch.start()
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen.on_object_selected(ObjectSelected(ref))
            await settle(pilot, rounds=10)
    finally:
        fetch.stop()
    calls = [call for call, _ in gateway.calls]
    assert calls.count("task_history") == 1
    assert calls.count("task_activity_history") == 1


async def test_a_schema_refresh_does_not_prime_what_was_already_fetched(repo) -> None:
    """Otherwise each schema list refresh would refetch what nobody looks at."""
    put_schemas(repo, "MYDB", "AAA")
    for object_type in (
        ObjectType.TABLE,
        ObjectType.VIEW,
        ObjectType.PIPE,
        ObjectType.STAGE,
        ObjectType.TASK,
    ):
        load(repo, objects_scope(object_type, "MYDB", "AAA"), [])
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        screen.post_message(CacheUpdated(schemas_scope("MYDB"), True))
        await settle(pilot)
    assert not STARTUP_PRIMES & set(fetch.scopes)


async def test_a_fresh_schema_list_with_unfetched_objects_is_still_primed(repo) -> None:
    """A fresh schema list is not fetched again, so no update will announce it."""
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.set_databases(["MYDB"])
        await settle(pilot)
    assert "schemas:MYDB" not in fetch.scopes
    assert STARTUP_PRIMES <= set(fetch.scopes)


async def test_a_schema_list_that_landed_behind_a_dialog_is_primed_when_it_closes(
    repo,
) -> None:
    """A cache update only reaches the main screen while it is on top."""
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        app.push_screen(MultiPickerDialog(["MYDB"], title="Databases"))
        await settle(pilot)
        put_schemas(repo, "MYDB", "AAA")
        assert not STARTUP_PRIMES & set(fetch.scopes)

        app.pop_screen()
        await settle(pilot)
    assert STARTUP_PRIMES <= set(fetch.scopes)


async def test_an_expand_overtakes_a_queued_startup_prime(repo) -> None:
    """The prime is at background priority; expanding asks for it at a higher
    one, which FetchService.request lets supersede what is queued."""
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await settle(pilot)
        assert ("tables@MYDB", int(Priority.BACKGROUND)) in fetch.requests

        tree = screen.query_one(ObjectTree)
        tree.root.children[0].expand()
        await pilot.pause()
        tree.root.children[0].children[0].expand()
        await settle(pilot)
    assert ("tables@MYDB", int(Priority.VISIBLE)) in fetch.requests


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
        screen.on_scope_needed(ScopeNeeded(misshapen, priority=Priority.INTERACTIVE))
        await settle(pilot)
    assert misshapen.key in fetch.scopes
    assert "tables@MYDB" not in fetch.scopes


async def test_a_repaints_request_is_dropped_if_the_scope_has_failed_since(
    repo,
) -> None:
    """The repaint asked while the scope's pass was running. Handled after that
    pass failed, nothing would stop the request from running the statement again."""
    scope = task_history_scope("MYDB", "PUBLIC", "NIGHTLY")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        repo.fail_pass(scope, repo.begin_pass(scope), "No active warehouse selected")
        app.screen.on_scope_needed(
            ScopeNeeded(scope, priority=Priority.INTERACTIVE, from_update=True)
        )
        await settle(pilot)
    assert scope.key not in fetch.scopes


async def test_coming_back_to_a_failed_scope_still_requests_it(repo) -> None:
    scope = task_history_scope("MYDB", "PUBLIC", "NIGHTLY")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        repo.fail_pass(scope, repo.begin_pass(scope), "No active warehouse selected")
        app.screen.on_scope_needed(ScopeNeeded(scope, priority=Priority.INTERACTIVE))
        await settle(pilot)
    assert scope.key in fetch.scopes


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


async def test_the_database_chord_opens_the_multipicker(repo) -> None:
    put_databases(repo, "ALPHA", "BETA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "a", "d")
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
        await pilot.press("comma", "a", "d")
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
        await pilot.press("comma", "a", "d")
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
        await pilot.press("comma", "a", "d")
        await settle(pilot)
        await pilot.click("#multipicker-all")
        await settle(pilot)
        await pilot.click("#multipicker-ok")
        await settle(pilot)
    assert store.load()[0].databases == ("ALPHA", "BETA")


async def test_picking_databases_keeps_the_profiles_name(repo, tmp_path) -> None:
    """The picker owns the database selection, not the name: confirming one
    must not wipe the label the user typed on the connect screen."""
    put_databases(repo, "ALPHA", "BETA")
    fetch = RecordingFetch()
    pool, _ = make_pool(size=1)
    pool.connect_all()
    store = ProfileStore(tmp_path / "recent.json")
    store.add(Profile(account="acct", user="user", name="Prod"))
    app = Harness(repo, fetch, pool=pool, profiles=store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "a", "d")
        await settle(pilot)
        await pilot.click("#multipicker-all")
        await settle(pilot)
        await pilot.click("#multipicker-ok")
        await settle(pilot)
    stored = store.load()[0]
    assert stored.name == "Prod"
    assert stored.databases == ("ALPHA", "BETA")


async def test_the_picker_is_not_opened_with_nothing_to_show(repo) -> None:
    """An empty dialog is a dead end; asking for the list is the useful act."""
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma", "a", "d")
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)
    assert fetch.scopes.count("databases") >= 2


# ----------------------------------------------------------------- refresh


async def test_refresh_targets_the_scope_under_the_cursor(repo) -> None:
    """Surgical, not 'fetch everything again': the node's own list and what
    the detail pane shows for it."""
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
    assert sorted(fetch.scopes) == ["grants:MYDB", "schemas:MYDB"]


async def test_refresh_on_a_schema_node_refreshes_the_list_it_is_in(repo) -> None:
    """A schema node owns no scope of its own, so F5 there used to do
    nothing -- not even drop a schema that was dropped elsewhere."""
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
        tree.move_cursor(tree.root.children[0].children[0])
        await settle(pilot)
        fetch.requests.clear()
        screen.action_refresh()
        await pilot.pause()
    assert sorted(fetch.scopes) == ["grants:MYDB.AAA", "schemas:MYDB"]


async def _cursor_on_category(pilot, screen, object_type: ObjectType, schema: str):
    """Expand down to a category of the first database and put the cursor on it."""
    tree = screen.query_one(ObjectTree)
    database_node = tree.root.children[0]
    database_node.expand()
    await pilot.pause()
    schema_node = next(n for n in database_node.children if n.data.key == schema)
    schema_node.expand()
    await pilot.pause()
    category = next(n for n in schema_node.children if n.data.key == object_type.value)
    tree.focus()
    tree.move_cursor(category)
    await settle(pilot)
    return category


@pytest.mark.parametrize("object_type", TREE_TYPES)
async def test_refresh_on_a_category_asks_for_its_own_schema_not_the_database(
    repo, object_type: ObjectType
) -> None:
    """A prime would succeed on a dropped schema and leave its node standing.
    The per-schema SHOW fails instead, which refreshes the schema list and
    removes the node at once."""
    put_databases(repo, "MYDB")
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        await _cursor_on_category(pilot, screen, object_type, "AAA")
        fetch.requests.clear()
        screen.action_refresh()
        await pilot.pause()
    assert objects_scope(object_type, "MYDB", "AAA").key in fetch.scopes
    assert prime_scope(object_type, "MYDB").key not in fetch.scopes


@pytest.mark.parametrize(
    ("object_type", "put"),
    [
        (ObjectType.PIPE, put_pipe),
        (ObjectType.STAGE, put_stage),
        (ObjectType.TASK, put_task),
        (ObjectType.TABLE, put_table),
        (ObjectType.VIEW, put_view),
    ],
)
async def test_refresh_on_an_object_refreshes_the_list_it_is_in(
    repo, object_type: ObjectType, put
) -> None:
    """Its view reads its SQL from that list, and only the list shows that a
    pipe was dropped: the statements a pipe's view sends succeed without it."""
    put_databases(repo, "MYDB")
    put_schemas(repo, "MYDB", "PUBLIC")
    put(repo, "THING")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        category = await _cursor_on_category(pilot, screen, object_type, "PUBLIC")
        category.expand()
        await pilot.pause()
        screen.query_one(ObjectTree).move_cursor(category.children[0])
        await settle(pilot)
        fetch.requests.clear()
        screen.action_refresh()
        await pilot.pause()
    assert objects_scope(object_type, "MYDB", "PUBLIC").key in fetch.scopes
    assert prime_scope(object_type, "MYDB").key not in fetch.scopes


async def test_refresh_with_nothing_under_the_cursor_asks_for_nothing(repo) -> None:
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


async def test_refresh_asks_for_the_detail_panes_scopes_while_they_are_fresh(
    repo,
) -> None:
    """F5 asks for what the detail pane shows whatever its age, so a pipe
    can be checked now rather than at the next sweep."""
    from nivaria.models import pipe_activity_scope
    from nivaria.ui.widgets.detail_pane import DetailPane
    from tests.views.harness import put_pipe, put_pipe_activity

    put_databases(repo, "MYDB")
    ref = put_pipe(repo, "LOAD_ORDERS")
    put_pipe_activity(repo, "LOAD_OTHER")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        screen.query_one(DetailPane).show(ref)
        await settle(pilot)
        fetch.requests.clear()
        screen.action_refresh()
        await pilot.pause()
    assert fetch.scopes == [
        objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC").key,
        pipe_activity_scope("MYDB", "PUBLIC").key,
    ]


async def test_the_sweep_leaves_a_view_alone_until_the_cursor_has_rested(repo) -> None:
    """A task never fetched is due at once, so the footer's tick would
    otherwise run a 90-day query for whichever row a held arrow key is on."""
    config.set(CURSOR_SETTLE_MS, 60_000)
    ref = put_task(repo, "NIGHTLY")
    history = task_activity_history_scope("MYDB", "PUBLIC", "NIGHTLY").key
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.on_object_selected(ObjectSelected(ref))
        await pilot.pause()
        screen._tick()
        await pilot.pause()
        assert history not in fetch.scopes
        screen.detail._cursor_rested()
        await pilot.pause()
        assert history in fetch.scopes


async def test_a_real_wait_ends_with_an_interactive_request(repo) -> None:
    """Through the real timer, which runs in the pane's message pump. Textual
    stops a message from bubbling past its sender, so a request sent from
    there would never reach this screen, and only the sweep would fetch it."""
    config.set(CURSOR_SETTLE_MS, 10)
    ref = put_task(repo, "NIGHTLY")
    history = task_activity_history_scope("MYDB", "PUBLIC", "NIGHTLY").key
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.on_object_selected(ObjectSelected(ref))
        await pilot.pause(0.2)
    assert (history, int(Priority.INTERACTIVE)) in fetch.requests


async def test_refresh_does_not_wait_for_the_cursor_to_rest(repo) -> None:
    """F5 is a request made on purpose."""
    config.set(CURSOR_SETTLE_MS, 60_000)
    ref = put_task(repo, "NIGHTLY")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.on_object_selected(ObjectSelected(ref))
        await pilot.pause()
        screen.action_refresh()
        await pilot.pause()
    assert task_activity_history_scope("MYDB", "PUBLIC", "NIGHTLY").key in fetch.scopes


async def test_moving_on_cancels_what_only_the_previous_view_was_waiting_on(
    repo,
) -> None:
    """Resting on each of ten tasks would otherwise queue ten 90-day
    statements, with the one on screen behind the rest."""
    first = put_task(repo, "FIRST")
    second = put_task(repo, "SECOND")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.on_object_selected(ObjectSelected(first))
        await pilot.pause()
        screen.on_object_selected(ObjectSelected(second))
        await pilot.pause()
    dropped = fetch.predicates[-1]
    assert dropped(task_activity_history_scope("MYDB", "PUBLIC", "FIRST"))
    assert dropped(task_history_scope("MYDB", "PUBLIC", "FIRST"))
    assert not dropped(task_activity_history_scope("MYDB", "PUBLIC", "SECOND"))


async def test_moving_on_keeps_the_listing_the_tree_shares(repo) -> None:
    """Moving from a list view to anything else must not take the list from
    under the tree, which reads it too."""
    put_task(repo, "NIGHTLY")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.on_category_selected(
            CategorySelected(objects_scope(ObjectType.TASK, "MYDB", "PUBLIC"))
        )
        await pilot.pause()
        screen.on_category_selected(
            CategorySelected(objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC"))
        )
        await pilot.pause()
    dropped = fetch.predicates[-1]
    assert dropped(task_activity_scope("MYDB", "PUBLIC"))
    assert not dropped(objects_scope(ObjectType.TASK, "MYDB", "PUBLIC"))


async def test_moving_between_two_pipes_keeps_their_shared_snapshot(repo) -> None:
    """Both views read the same schema-wide statement, so it stays queued."""
    first = put_pipe(repo, "FIRST")
    second = put_pipe(repo, "SECOND")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.on_object_selected(ObjectSelected(first))
        await pilot.pause()
        screen.on_object_selected(ObjectSelected(second))
        await pilot.pause()
    for dropped in fetch.predicates:
        assert not dropped(pipe_activity_scope("MYDB", "PUBLIC"))


async def test_a_full_refresh_forgets_coverage_of_the_log_scopes_on_screen(
    repo,
) -> None:
    """The escape hatch for a row a window pass missed: the detail pane's
    log scopes are asked for again, each with its coverage forgotten so the
    pass is a full one. Other scopes on screen, here the SHOW PIPES list,
    are left alone."""
    from nivaria.models import ObjectType, objects_scope, pipe_activity_scope
    from nivaria.ui.widgets.detail_pane import DetailPane
    from tests.views.harness import put_pipe

    put_databases(repo, "MYDB")
    put_pipe(repo, "LOAD_ORDERS")
    activity = pipe_activity_scope("MYDB", "PUBLIC")
    generation = repo.begin_pass(activity)
    repo.finish_pass(activity, generation, covered_from=1.0)
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        detail = screen.query_one(DetailPane)
        detail.show_category(objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC"))
        await settle(pilot)
        assert len(detail.scopes()) > 1
        fetch.requests.clear()
        screen.full_refresh()
        await pilot.pause()
    assert fetch.scopes == [activity.key]
    assert repo.entry(activity).covered_from is None


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
    from nivaria.settings import CACHE_TTL, config

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
    from nivaria.ui.screens.search import SearchDialog

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
    used to (see SchemaRef and docs/DESIGN.md's "Choosing a hit")."""
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


# ------------------------------------------------------- highlight vs. jump


async def test_a_search_jump_above_the_highlighted_object_settles_on_the_hit(
    repo,
) -> None:
    """The jump expands a schema above the highlighted table, which shifts
    that table's line, and Textual re-highlights a node whose line shifted.
    When a highlight also revealed, that re-highlight queued a cursor move
    of its own, and the two moves chased each other forever: the tree
    scrolled by itself and the detail pane kept being re-pointed."""
    put_schemas(repo, "MYDB", "AAA", "ZZZ")
    put_tables(repo, "MYDB", "AAA", *[f"A{i:02d}" for i in range(20)])
    put_tables(repo, "MYDB", "ZZZ", *[f"Z{i:02d}" for i in range(20)])
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await settle(pilot)
        screen._select(ObjectRef(ObjectType.TABLE, "MYDB", "ZZZ", "Z10"))
        await settle(pilot)

        screen._select(ObjectRef(ObjectType.TABLE, "MYDB", "AAA", "A05"))
        await settle(pilot)
        shown: list[ObjectRef] = []
        show = screen.detail.show

        def recording_show(ref: ObjectRef):
            shown.append(ref)
            return show(ref)

        screen.detail.show = recording_show
        await settle(pilot)

        tree = screen.query_one(ObjectTree)
        assert tree.cursor_node.data.key == "A05"
        assert screen.detail.ref.name == "A05"
        assert shown == []


async def test_enter_on_a_list_row_moves_the_tree_cursor_to_that_object(
    repo,
) -> None:
    """Left on the category node, the tree names nothing the Detail pane
    shows. Enter in a list is a jump from outside the tree, like a search
    hit, so it reveals."""
    from textual.widgets import DataTable

    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS", "PAYMENTS")
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        tree.root.children[0].expand()
        await pilot.pause()
        schema = tree.root.children[0].children[0]
        schema.expand()
        await pilot.pause()
        tree.move_cursor(schema.children[0])
        await settle(pilot)

        screen.query_one("#detail-list", DataTable).focus()
        await pilot.pause()
        await pilot.press("down", "enter")
        await settle(pilot)

        assert tree.cursor_node.data.key == "PAYMENTS"
        assert screen.detail.ref.name == "PAYMENTS"


async def test_highlighting_an_object_in_the_tree_does_not_reveal_it_again(
    repo, monkeypatch
) -> None:
    """The cursor is already on it; revealing is for a jump from outside."""
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS", "PAYMENTS")
    revealed: list[str] = []
    original = ObjectTree.reveal

    def spy(self, ref):
        revealed.append(ref.name)
        return original(self, ref)

    monkeypatch.setattr(ObjectTree, "reveal", spy)
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        tree, tables = await drill_to_tables(app, pilot, screen)
        tree.move_cursor(tables.children[1])
        await settle(pilot)

        assert screen.detail.ref.name == "PAYMENTS"
    assert revealed == []


# -------------------------------------------------------------- snowsight

SNOWSIGHT = "https://app.snowflake.com/myorg/myacct/"


def snowsight_pool(
    account: str = "MYORG-MYACCT", names: AccountNames | None = None
) -> ConnectionPool:
    """A pool as connect_all leaves it, without connecting: the harness never
    calls it, so the names it would have read are set directly, and so is the
    settings instance NivariaApp.switch_repo would have set."""
    config.current_instance = account.casefold()
    pool = ConnectionPool(
        ConnectParams(account=account, user="user"), 1, connector=FakeConnector()
    )
    pool.account_names = names or AccountNames("MYORG", "MYACCT")
    return pool


async def press_on(app, pilot, pick, *keys) -> list[str]:
    """Move the tree cursor to `pick(tree, tables)`, press `keys`, return what
    would have been opened."""
    opened: list[str] = []
    app.open_url = lambda url, **_: opened.append(url)
    screen = app.screen
    screen.set_databases(["MYDB"])
    await pilot.pause()
    tree, tables = await drill_to_tables(app, pilot, screen)
    tree.focus()
    tree.move_cursor(pick(tree, tables))
    await settle(pilot)
    await pilot.press(*keys)
    await pilot.pause()
    return opened


async def open_object_on(app, pilot, pick) -> list[str]:
    return await press_on(app, pilot, pick, "comma", "w", "o")


async def open_workspaces(app, pilot) -> list[str]:
    """Press the chord on an empty tree: it needs no cursor."""
    opened: list[str] = []
    app.open_url = lambda url, **_: opened.append(url)
    await pilot.press("comma", "w", "w")
    await pilot.pause()
    return opened


async def test_the_object_chord_opens_the_highlighted_object_in_snowsight(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo, RecordingFetch(), pool=snowsight_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        opened = await open_object_on(
            app, pilot, lambda tree, tables: tables.children[0]
        )
    assert opened == [f"{SNOWSIGHT}#/data/databases/MYDB/schemas/AAA/table/ORDERS"]


async def test_the_object_chord_on_a_database_opens_the_database(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo, RecordingFetch(), pool=snowsight_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        opened = await open_object_on(
            app, pilot, lambda tree, tables: tree.root.children[0]
        )
    assert opened == [f"{SNOWSIGHT}#/data/databases/MYDB"]


async def test_the_object_chord_on_a_category_opens_its_schema(repo) -> None:
    """Snowsight has no page for one kind of object in a schema."""
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo, RecordingFetch(), pool=snowsight_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        opened = await open_object_on(app, pilot, lambda tree, tables: tables)
    assert opened == [f"{SNOWSIGHT}#/data/databases/MYDB/schemas/AAA"]


async def test_the_object_chord_opens_the_accounts_snowsight_url_setting(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    pool = snowsight_pool("MYORG-MYACCT.privatelink")
    config.set(SNOWSIGHT_URL, "https://snowsight.example/myorg/myacct")
    app = Harness(repo, RecordingFetch(), pool=pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        opened = await open_object_on(
            app, pilot, lambda tree, tables: tree.root.children[0]
        )
    assert opened == ["https://snowsight.example/myorg/myacct/#/data/databases/MYDB"]


def capture_notices(app) -> list[str]:
    notices: list[str] = []
    original = app.screen.notify
    app.screen.notify = lambda message, **kw: (
        notices.append(message),
        original(message, **kw),
    )
    return notices


async def test_the_object_chord_on_a_privatelink_account_without_the_setting_asks_for_it(
    repo,
) -> None:
    """Guessing a private host opened a dead link on a real account."""
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(
        repo, RecordingFetch(), pool=snowsight_pool("MYORG-MYACCT.privatelink")
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        notices = capture_notices(app)
        opened = await open_object_on(
            app, pilot, lambda tree, tables: tree.root.children[0]
        )
    assert opened == []
    assert notices and "Settings" in notices[0]


async def test_a_plain_w_opens_nothing(repo) -> None:
    """Opening an object moved under `, w`; `w` alone is not a shortcut for it."""
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    app = Harness(repo, RecordingFetch(), pool=snowsight_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        opened = await press_on(
            app, pilot, lambda tree, tables: tree.root.children[0], "w"
        )
    assert opened == []


async def test_the_workspaces_chord_opens_workspaces_with_no_tree_cursor(repo) -> None:
    app = Harness(repo, RecordingFetch(), pool=snowsight_pool())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        opened = await open_workspaces(app, pilot)
    assert opened == [f"{SNOWSIGHT}#/workspaces"]


async def test_the_workspaces_chord_opens_the_accounts_snowsight_url_setting(
    repo,
) -> None:
    pool = snowsight_pool("MYORG-MYACCT.privatelink")
    config.set(SNOWSIGHT_URL, "https://snowsight.example/myorg/myacct")
    app = Harness(repo, RecordingFetch(), pool=pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        opened = await open_workspaces(app, pilot)
    assert opened == ["https://snowsight.example/myorg/myacct/#/workspaces"]


async def test_the_workspaces_chord_on_a_privatelink_account_without_the_setting_asks_for_it(
    repo,
) -> None:
    app = Harness(
        repo, RecordingFetch(), pool=snowsight_pool("MYORG-MYACCT.privatelink")
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        notices = capture_notices(app)
        opened = await open_workspaces(app, pilot)
    assert opened == []
    assert notices and "Settings" in notices[0]


async def test_the_object_chord_without_the_account_names_asks_for_the_setting(
    repo,
) -> None:
    """The names are read at connect, best effort; if that failed there is
    nothing to build a link from."""
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    pool = snowsight_pool()
    pool.account_names = None
    app = Harness(repo, RecordingFetch(), pool=pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        notices = capture_notices(app)
        opened = await open_object_on(
            app, pilot, lambda tree, tables: tree.root.children[0]
        )
    assert opened == []
    assert notices and "Settings" in notices[0]


# ------------------------------------------------------------------ journal


def journal_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "nivaria.journal"]


async def test_the_startup_lists_are_asked_for_as_startup(repo) -> None:
    fetch = RecordingFetch()
    async with Harness(repo, fetch).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
    assert {
        ("databases", "startup"),
        ("warehouses", "startup"),
        ("roles", "startup"),
    } <= set(fetch.reasons)


async def test_an_added_database_is_primed_as_database_added(repo, caplog) -> None:
    put_schemas(repo, "MYDB", "AAA")
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.set_databases(["MYDB"])
        await settle(pilot)
    assert ("tables@MYDB", "database added") in fetch.reasons
    assert "user  databases added: MYDB" in journal_lines(caplog)


async def test_an_expanded_node_asks_as_expanded_and_is_journalled(
    repo, caplog
) -> None:
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
    assert ("tables@MYDB", "expanded") in fetch.reasons
    assert "user  expanded schema MYDB.AAA" in journal_lines(caplog)


async def test_f5_asks_for_everything_on_screen_as_f5(repo, caplog) -> None:
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
        tree.focus()
        tree.move_cursor(tree.root.children[0])
        await settle(pilot)
        fetch.reasons.clear()
        screen.action_refresh()
        await pilot.pause()
    assert sorted(fetch.reasons) == [("grants:MYDB", "f5"), ("schemas:MYDB", "f5")]
    assert "user  pressed F5" in journal_lines(caplog)


async def test_a_sweep_says_how_much_was_stale_and_asks_as_sweep(repo, caplog) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    config.set(CACHE_TTL, 60)
    fetch = RecordingFetch()
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        await drill_to_tables(app, pilot, screen)
        await settle(pilot)
        with repo._db.connect() as connection:
            connection.execute(
                "UPDATE cache_entry SET fetched_at = fetched_at - 999 WHERE scope = ?",
                (objects_scope(ObjectType.TABLE, "MYDB", "AAA").key,),
            )
        fetch.reasons.clear()
        fetch.busy.clear()
        screen._sweep()
        await pilot.pause()
    assert ("tables@MYDB", "sweep") in fetch.reasons
    assert any(
        line.startswith("note  sweep: ") and line.endswith("cache age 1m")
        for line in journal_lines(caplog)
    )


async def test_a_sweeps_result_repaints_without_promoting_the_sweep(repo) -> None:
    """A repaint answering a background fetch asks at background, so a sweep's
    own result cannot make its other requests log in with nobody there."""
    put_schemas(repo, "MYDB", "AAA")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
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
        fetch.requests.clear()
        fetch.busy.clear()
        load(repo, columns_scope("MYDB", "AAA", "ORDERS"), [])
        screen.post_message(
            CacheUpdated(
                columns_scope("MYDB", "AAA", "ORDERS"), True, Priority.BACKGROUND
            )
        )
        await settle(pilot)
    assert (
        "column_tag_references:MYDB.AAA.ORDERS",
        int(Priority.BACKGROUND),
    ) in fetch.requests
