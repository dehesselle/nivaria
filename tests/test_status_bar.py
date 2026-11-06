# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The footer's status corner: staleness countdown and pool gauge."""

from __future__ import annotations

from math import ceil
from pathlib import Path
from types import SimpleNamespace

import pytest
from tcoda.chords import ChordMixin
from tcoda.widgets import Countdown, Footer, FooterBinding
from textual.app import App

import nivaria.cache.repository as repository_module
import nivaria.ui.app as app_module
from nivaria.cache.db import Database
from nivaria.cache.repository import Repository
from nivaria.models import ObjectType, objects_scope, schemas_scope
from nivaria.settings import CACHE_TTL, config
from nivaria.ui.screens.main import MainScreen
from nivaria.ui.widgets.object_tree import ObjectTree
from nivaria.ui.widgets.status_bar import PoolGauge, RoleLabel, WarehouseLabel
from tests.fakes import make_pool
from tests.test_main_screen import RecordingFetch, put_schemas, put_tables, settle


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(ChordMixin, App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, fetch, pool=None) -> None:
        super().__init__()
        self._repo = repo
        self._fetch = fetch
        self._pool = pool

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self._repo, self._fetch, self._pool))


class Clock:
    """A hand-moved clock for the cache ledger, which the countdown reads."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    # The module's own reference, not time.time itself: Textual and asyncio
    # run on the real clock and must keep doing so.
    monkeypatch.setattr(repository_module, "time", SimpleNamespace(time=clock))
    return clock


def load(repo: Repository, scope, rows) -> None:
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


def age_by(repo: Repository, scope, seconds: float) -> None:
    """Backdate a scope so it reads as stale.

    Rewinding the timestamp rather than sleeping keeps these tests instant and
    deterministic -- a real sleep would also have to block the event loop the
    app under test is running on.
    """
    with repo._db.connect() as connection:
        connection.execute(
            "UPDATE cache_entry SET fetched_at = fetched_at - ? WHERE scope = ?",
            (seconds, scope.key),
        )


# ------------------------------------------------------- rendering, in the small


def test_the_countdown_has_no_leading_zeroes() -> None:
    """Asked for explicitly: three digits, right aligned, no padding zeros."""
    countdown = Countdown()
    countdown.remaining = 7
    assert countdown.render() == "7"
    countdown.remaining = 42
    assert countdown.render() == "42"
    countdown.remaining = 900
    assert countdown.render() == "900"


def test_the_countdown_is_capped_at_three_digits() -> None:
    countdown = Countdown()
    countdown.remaining = 98765
    assert countdown.render() == "999"


# "Blanks when disabled" and the spinner's own frame-by-frame advance are
# tcoda's business now -- see its tests/test_countdown.py. Disabling is no
# longer a blank render either: `test_a_ttl_of_zero_disables_the_countdown`
# below asserts the widget is hidden, which is what keeps a real zero
# distinguishable from a countdown that is switched off.


def test_the_gauge_reads_in_use_over_size() -> None:
    gauge = PoolGauge()
    gauge.in_use = 2
    gauge.pool_size = 5
    assert gauge.render() == "(2/5)"


def test_the_gauge_is_blank_without_a_pool() -> None:
    assert PoolGauge().render() == ""


# ------------------------------------------------------------------ wiring


async def test_the_footer_carries_both_indicators(repo) -> None:
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        footer = app.screen.query_one(Footer)
        assert footer.query_one("#footer-countdown", Countdown)
        assert footer.query_one("#footer-pool", PoolGauge)


async def test_the_role_label_appears_before_the_warehouse_label(repo) -> None:
    """Requested order: role, then warehouse, left of the countdown/gauge."""
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        status = app.screen.query_one(Footer).status
        ids = [child.id for child in status.children if child.id]
    assert ids.index("footer-role") < ids.index("footer-warehouse")


async def test_the_warehouse_label_widens_for_a_longer_name(repo) -> None:
    """Regression: these labels are width: auto, and a plain reactive only
    repaints in place -- a longer value stayed clipped at whatever width the
    first one happened to need until something unrelated forced a relayout."""
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        label = app.screen.query_one(WarehouseLabel)
        label.warehouse = "WH"
        await pilot.pause()
        short_width = label.size.width

        label.warehouse = "A_MUCH_LONGER_WAREHOUSE_NAME"
        await pilot.pause()
        long_width = label.size.width
    assert long_width > short_width


async def test_the_role_label_widens_for_a_longer_name(repo) -> None:
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        label = app.screen.query_one(RoleLabel)
        label.role = "PUBLIC"
        await pilot.pause()
        short_width = label.size.width

        label.role = "A_MUCH_LONGER_ROLE_NAME"
        await pilot.pause()
        long_width = label.size.width
    assert long_width > short_width


async def test_the_command_palette_key_does_not_claim_the_corner(repo) -> None:
    """It gets no special treatment in tcoda's footer -- it is an app binding
    like any other, and it is declared `show=False`, so it simply is not
    rendered. The right corner is the status area's."""
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        actions = {key.action for key in app.screen.query(FooterBinding)}
    assert "command_palette" not in actions


async def test_key_bindings_are_still_shown(repo) -> None:
    """Replacing the footer must not cost the ordinary binding rendering."""
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        # Only the *keys* are rebuilt when bindings land, and that costs a
        # turn for the signal plus one for the mounts.
        for _ in range(4):
            await pilot.pause()
        keys = {key.key for key in app.screen.query(FooterBinding)}
    assert "comma" in keys
    assert "slash" in keys
    assert "f5" in keys


async def test_the_chords_are_hidden_from_the_footer_behind_one_hint(repo) -> None:
    """They are found in the chord viewer. The footer only says how to get
    there, from its left corner."""
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        keys = [key.key for key in app.screen.query(FooterBinding)]
    assert keys[0] == "comma"
    assert not [key for key in keys if " " in key]


async def test_the_gauge_follows_the_pool(repo) -> None:
    pool, _ = make_pool(size=3)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        gauge = app.screen.query_one(PoolGauge)
        assert (gauge.in_use, gauge.pool_size) == (0, 3)

        with pool.acquire():
            app.screen._tick()
            await pilot.pause()
            assert app.screen.query_one(PoolGauge).in_use == 1

        app.screen._tick()
        await pilot.pause()
        assert app.screen.query_one(PoolGauge).in_use == 0


# ------------------------------------------------------------- the countdown


async def test_the_countdown_describes_the_focused_scope(repo) -> None:
    """Not a global timer: it has to change as you move around."""
    put_schemas(repo, "MYDB", "AAA")
    load(
        repo,
        objects_scope(ObjectType.TABLE, "MYDB", "AAA"),
        [
            {
                "database_name": "MYDB",
                "schema_name": "AAA",
                "name": "ORDERS",
                "kind": "TABLE",
                "comment": "",
            }
        ],
    )
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.set_databases(["MYDB"])
        await pilot.pause()
        tree = screen.query_one(ObjectTree)
        node = tree.root.children[0]
        node.expand()
        await pilot.pause()
        tree.move_cursor(node)
        await pilot.pause()
        assert screen.focused_scope() == schemas_scope("MYDB")

        schema = node.children[0]
        schema.expand()
        await pilot.pause()
        tree.move_cursor(schema.children[0])
        await pilot.pause()
        assert screen.focused_scope() == objects_scope(ObjectType.TABLE, "MYDB", "AAA")


async def test_an_object_leaf_reports_the_list_it_came_from(repo) -> None:
    """A leaf owns no scope; the interesting age is that of its category."""
    put_schemas(repo, "MYDB", "AAA")
    load(
        repo,
        objects_scope(ObjectType.TABLE, "MYDB", "AAA"),
        [
            {
                "database_name": "MYDB",
                "schema_name": "AAA",
                "name": "ORDERS",
                "kind": "TABLE",
                "comment": "",
            }
        ],
    )
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
        tables = schema.children[0]
        tables.expand()
        await pilot.pause()
        tree.move_cursor(tables.children[0])
        await pilot.pause()
        assert screen.focused_scope() == objects_scope(ObjectType.TABLE, "MYDB", "AAA")


async def countdown_after_tick(app, pilot) -> int:
    app.screen._tick()
    await pilot.pause()
    return app.screen.query_one(Countdown).remaining


async def test_the_countdown_shows_when_the_first_watched_scope_goes_stale(
    repo, clock
) -> None:
    put_schemas(repo, "MYDB", "AAA")
    put_schemas(repo, "OTHER", "BBB")
    age_by(repo, schemas_scope("MYDB"), 20)
    age_by(repo, schemas_scope("OTHER"), 45)
    config.set(CACHE_TTL, 60)
    try:
        app = Harness(repo, RecordingFetch())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen.set_databases(["MYDB", "OTHER"])
            await pilot.pause()
            assert await countdown_after_tick(app, pilot) == 15
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_with_nothing_watched_the_countdown_shows_the_full_ttl(repo) -> None:
    config.set(CACHE_TTL, 60)
    try:
        app = Harness(repo, RecordingFetch())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert await countdown_after_tick(app, pilot) == 60
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_a_scope_being_fetched_does_not_hold_the_countdown_at_zero(
    repo, clock
) -> None:
    """Its old fetched_at reads as overdue until the pass lands; the spinner
    covers that wait."""
    put_schemas(repo, "MYDB", "AAA")
    put_schemas(repo, "OTHER", "BBB")
    age_by(repo, schemas_scope("MYDB"), 100)
    age_by(repo, schemas_scope("OTHER"), 20)
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Showing MYDB stale requests it at once, and it stays queued.
            app.screen.set_databases(["MYDB", "OTHER"])
            await pilot.pause()
            assert "schemas:MYDB" in fetch.busy
            assert await countdown_after_tick(app, pilot) == 40
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_a_failed_refresh_does_not_hold_the_countdown_at_zero(
    repo, clock
) -> None:
    """It keeps its old fetched_at, so counting it would pin the countdown at
    zero and rerun the failing statement on every tick."""
    put_schemas(repo, "MYDB", "AAA")
    put_schemas(repo, "OTHER", "BBB")
    failed = schemas_scope("MYDB")
    age_by(repo, failed, 100)
    repo.fail_pass(failed, repo.begin_pass(failed), "no warehouse")
    age_by(repo, schemas_scope("OTHER"), 20)
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen.set_databases(["MYDB", "OTHER"])
            await pilot.pause()
            # Showing it retried it at once; that retry failed too.
            fetch.busy.clear()
            fetch.requests.clear()
            assert await countdown_after_tick(app, pilot) == 40
            assert fetch.scopes == []
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_a_failed_scope_is_not_retried_by_a_sweep_another_scope_triggers(
    repo, clock
) -> None:
    """Scopes age on their own clocks, so sweeps come often. Each one used to
    rerun every failed statement on screen, whichever scope it was for."""
    put_schemas(repo, "MYDB", "AAA")
    put_schemas(repo, "OTHER", "BBB")
    failed = schemas_scope("MYDB")
    age_by(repo, failed, 100)
    repo.fail_pass(failed, repo.begin_pass(failed), "no warehouse")
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.set_databases(["MYDB", "OTHER"])
            await pilot.pause()
            fetch.busy.clear()
            fetch.requests.clear()

            clock.now += 30
            age_by(repo, schemas_scope("OTHER"), 100)
            screen._sweep()
            await pilot.pause()
            assert fetch.scopes == ["schemas:OTHER"]

            fetch.busy.clear()
            fetch.requests.clear()
            clock.now += 30
            screen._sweep()
            await pilot.pause()
            assert "schemas:MYDB" in fetch.scopes
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_with_only_failed_scopes_on_screen_the_countdown_runs_to_their_retry(
    repo, clock
) -> None:
    put_schemas(repo, "MYDB", "AAA")
    failed = schemas_scope("MYDB")
    age_by(repo, failed, 100)
    repo.fail_pass(failed, repo.begin_pass(failed), "no warehouse")
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen.set_databases(["MYDB"])
            await pilot.pause()
            fetch.busy.clear()
            fetch.requests.clear()
            assert await countdown_after_tick(app, pilot) == 60
            clock.now += 15
            assert await countdown_after_tick(app, pilot) == 45
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_a_warehouse_switch_retries_what_failed_on_screen(repo, clock) -> None:
    """Without a warehouse every SELECT-backed scope fails. Left to the sweep,
    they would wait a whole TTL after the warehouse that fixes them is set."""
    from nivaria.service.jobs import Priority

    put_schemas(repo, "MYDB", "AAA")
    failed = schemas_scope("MYDB")
    repo.fail_pass(failed, repo.begin_pass(failed), "no warehouse")
    pool, _ = make_pool(size=1)
    pool.connect_all()
    fetch = RecordingFetch()
    app = Harness(repo, fetch, pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.set_databases(["MYDB"])
        await pilot.pause()
        fetch.busy.clear()
        fetch.requests.clear()
        app.screen._switch_warehouse("COMPUTE_WH")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert dict(fetch.requests)["schemas:MYDB"] == int(Priority.VISIBLE)


async def test_nothing_on_screen_goes_stale_before_the_countdown_reaches_zero(
    repo, clock
) -> None:
    """A sweep's refresh lands a moment after the sweep. A countdown restarted
    at the full TTL therefore ran out just before that data went stale,
    skipped it, and left it on screen for nearly two TTLs."""
    put_schemas(repo, "MYDB", "AAA")
    scope = schemas_scope("MYDB")
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.set_databases(["MYDB"])
            await pilot.pause()
            fetch.requests.clear()

            def tick_until_requested() -> float:
                for _ in range(200):
                    clock.now += 1
                    screen._tick()
                    if scope.key in fetch.scopes:
                        fetch.requests.clear()
                        return clock.now
                    due_in = ceil(60 - repo.entry(scope).age())
                    shown = screen.query_one(Countdown).remaining
                    assert shown <= due_in, (
                        f"countdown shows {shown}s, data goes stale in {due_in}s"
                    )
                pytest.fail("the stale scope was never requested")

            loaded = clock.now
            assert tick_until_requested() == loaded + 60

            clock.now += 2  # the refresh lands
            load(repo, scope, [{"database_name": "MYDB", "name": "AAA", "comment": ""}])
            fetch.busy.discard(scope.key)
            landed = clock.now
            assert tick_until_requested() == landed + 60
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_a_ttl_of_zero_disables_the_countdown(repo) -> None:
    config.set(CACHE_TTL, 0)
    try:
        app = Harness(repo, RecordingFetch())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen._tick()
            await pilot.pause()
            countdown = app.screen.query_one(Countdown)
            # Hidden, not blanked: the widget still holds a number, so "off"
            # and "zero seconds left" stay distinguishable.
            assert countdown.display is False
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_the_spinner_shows_while_the_focused_scope_is_busy(repo) -> None:
    fetch = RecordingFetch()
    fetch.busy.add("databases")
    app = Harness(repo, fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen._tick()
        await pilot.pause()
        assert app.screen.query_one(Countdown).refreshing is True


# ------------------------------------------------------------------ sweep


async def test_the_sweep_refreshes_only_stale_visible_scopes(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.set_databases(["MYDB"])
            await pilot.pause()
            fetch.requests.clear()
            screen._sweep()
            await pilot.pause()
            assert fetch.scopes == []  # still fresh

            age_by(repo, schemas_scope("MYDB"), 120)
            screen._sweep()
            await pilot.pause()
            assert "schemas:MYDB" in fetch.scopes
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_the_sweep_requests_at_background_priority(repo) -> None:
    """Nothing it finds is urgent by definition; anything urgent was asked for
    interactively and must not be queued behind a sweep."""
    from nivaria.service.jobs import Priority

    put_schemas(repo, "MYDB", "AAA")
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen.set_databases(["MYDB"])
            await pilot.pause()
            fetch.requests.clear()
            age_by(repo, schemas_scope("MYDB"), 120)
            app.screen._sweep()
            await pilot.pause()
        assert dict(fetch.requests)["schemas:MYDB"] == int(Priority.BACKGROUND)
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_the_sweep_refreshes_a_schema_listing_through_the_database_prime(
    repo,
) -> None:
    """Expanded schemas were filled by one prime and go stale together;
    requesting each on its own would cost a round trip per schema and
    category."""
    put_schemas(repo, "MYDB", "AAA", "BBB")
    put_tables(repo, "MYDB", "AAA", "ORDERS")
    put_tables(repo, "MYDB", "BBB", "ITEMS")
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.set_databases(["MYDB"])
            await pilot.pause()
            tree = screen.query_one(ObjectTree)
            database = tree.root.children[0]
            database.expand()
            await pilot.pause()
            for schema in database.children:
                schema.expand()
            await settle(pilot)
            fetch.requests.clear()
            fetch.busy.clear()
            age_by(repo, objects_scope(ObjectType.TABLE, "MYDB", "AAA"), 120)
            age_by(repo, objects_scope(ObjectType.TABLE, "MYDB", "BBB"), 120)
            screen._sweep()
            await pilot.pause()
        assert "tables@MYDB" in fetch.scopes
        assert "tables:MYDB.AAA" not in fetch.scopes
        assert "tables:MYDB.BBB" not in fetch.scopes
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_a_ttl_of_zero_disables_the_sweep(repo) -> None:
    put_schemas(repo, "MYDB", "AAA")
    config.set(CACHE_TTL, 0)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen.set_databases(["MYDB"])
            await pilot.pause()
            fetch.requests.clear()
            app.screen._sweep()
            await pilot.pause()
        assert fetch.scopes == []
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)
