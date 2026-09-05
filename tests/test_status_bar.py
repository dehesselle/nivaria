"""The footer's status corner: staleness countdown and pool gauge."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Footer

import snowcone.ui.app as app_module
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import ObjectKind, databases_scope, objects_scope, schemas_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.screens.main import MainScreen
from snowcone.ui.widgets.object_tree import ObjectTree
from snowcone.ui.widgets.status_bar import (
    Countdown,
    PoolGauge,
    RoleLabel,
    StatusFooter,
    WarehouseLabel,
)
from tests.fakes import make_pool
from tests.test_main_screen import RecordingFetch, put_schemas


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


class Harness(App[None]):
    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, repo: Repository, fetch, pool=None) -> None:
        super().__init__()
        self._repo = repo
        self._fetch = fetch
        self._pool = pool

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self._repo, self._fetch, self._pool))


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


def test_the_countdown_blanks_when_disabled() -> None:
    """A TTL of 0 turns background refreshing off; the counter has nothing to
    count down to and must not imply otherwise."""
    countdown = Countdown()
    countdown.enabled = False
    countdown.remaining = 30
    assert countdown.render() == ""


def test_a_refreshing_scope_shows_a_spinner_instead_of_a_number() -> None:
    countdown = Countdown()
    countdown.remaining = 30
    countdown.refreshing = True
    first = countdown.render()
    countdown.frame = 1
    assert first != countdown.render()
    assert first.strip() != ""


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
        footer = app.screen.query_one(StatusFooter)
        assert footer.query_one("#footer-countdown", Countdown)
        assert footer.query_one("#footer-pool", PoolGauge)


async def test_the_role_label_appears_before_the_warehouse_label(repo) -> None:
    """Requested order: role, then warehouse, left of the countdown/gauge."""
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        footer = app.screen.query_one(StatusFooter)
        ids = [child.id for child in footer.children if child.id]
    assert ids.index("footer-role") < ids.index("footer-warehouse")


async def test_the_warehouse_label_widens_for_a_longer_name(repo) -> None:
    """Regression: these labels are width: auto, and a plain reactive only
    repaints in place -- a longer value stayed clipped at whatever width the
    first one happened to need until something unrelated forced a relayout."""
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        # The footer recomposes via call_after_refresh once its bindings are
        # ready (see test_key_bindings_are_still_shown); a single pause can
        # catch it still empty, so #footer-warehouse would not exist yet.
        for _ in range(4):
            await pilot.pause()
        footer = app.screen.query_one(StatusFooter)
        footer.warehouse = "WH"
        await pilot.pause()
        short_width = footer.query_one(WarehouseLabel).size.width

        footer.warehouse = "A_MUCH_LONGER_WAREHOUSE_NAME"
        await pilot.pause()
        long_width = footer.query_one(WarehouseLabel).size.width
    assert long_width > short_width


async def test_the_role_label_widens_for_a_longer_name(repo) -> None:
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        footer = app.screen.query_one(StatusFooter)
        footer.role = "PUBLIC"
        await pilot.pause()
        short_width = footer.query_one(RoleLabel).size.width

        footer.role = "A_MUCH_LONGER_ROLE_NAME"
        await pilot.pause()
        long_width = footer.query_one(RoleLabel).size.width
    assert long_width > short_width


async def test_the_command_palette_key_does_not_claim_the_corner(repo) -> None:
    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.screen.query_one(Footer).show_command_palette is False


async def test_key_bindings_are_still_shown(repo) -> None:
    """Subclassing Footer rather than replacing it is what keeps these."""
    from textual.widgets._footer import FooterKey

    app = Harness(repo, RecordingFetch())
    async with app.run_test(size=(120, 40)) as pilot:
        # The footer recomposes via call_after_refresh once its bindings are
        # ready, so a single pause can catch it still empty.
        for _ in range(4):
            await pilot.pause()
        keys = {key.key for key in app.screen.query(FooterKey)}
    assert "ctrl+d" in keys
    assert "slash" in keys


async def test_the_gauge_follows_the_pool(repo) -> None:
    pool, _ = make_pool(size=3)
    pool.connect_all()
    app = Harness(repo, RecordingFetch(), pool)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        footer = app.screen.query_one(StatusFooter)
        assert (footer.in_use, footer.pool_size) == (0, 3)

        with pool.acquire():
            app.screen._tick()
            await pilot.pause()
            assert app.screen.query_one(StatusFooter).in_use == 1

        app.screen._tick()
        await pilot.pause()
        assert app.screen.query_one(StatusFooter).in_use == 0


# ------------------------------------------------------------- the countdown


async def test_the_countdown_describes_the_focused_scope(repo) -> None:
    """Not a global timer: it has to change as you move around."""
    put_schemas(repo, "MYDB", "AAA")
    load(
        repo,
        objects_scope(ObjectKind.TABLE, "MYDB", "AAA"),
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
        screen.set_database("MYDB")
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
        assert screen.focused_scope() == objects_scope(ObjectKind.TABLE, "MYDB", "AAA")


async def test_an_object_leaf_reports_the_list_it_came_from(repo) -> None:
    """A leaf owns no scope; the interesting age is that of its category."""
    put_schemas(repo, "MYDB", "AAA")
    load(
        repo,
        objects_scope(ObjectKind.TABLE, "MYDB", "AAA"),
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
        screen.set_database("MYDB")
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
        assert screen.focused_scope() == objects_scope(ObjectKind.TABLE, "MYDB", "AAA")


async def test_the_countdown_counts_down_from_the_ttl(repo) -> None:
    load(repo, databases_scope(), [{"name": "MYDB", "comment": ""}])
    config.set(CACHE_TTL, 60)
    try:
        app = Harness(repo, RecordingFetch())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen._tick()
            await pilot.pause()
            remaining = app.screen.query_one(StatusFooter).remaining
        assert 55 <= remaining <= 60
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)


async def test_a_stale_scope_counts_zero_rather_than_negative(repo) -> None:
    load(repo, databases_scope(), [{"name": "MYDB", "comment": ""}])
    age_by(repo, databases_scope(), 120)
    config.set(CACHE_TTL, 60)
    try:
        app = Harness(repo, RecordingFetch())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen._tick()
            await pilot.pause()
            assert app.screen.query_one(StatusFooter).remaining == 0
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
            footer = app.screen.query_one(StatusFooter)
            assert footer.countdown_enabled is False
            assert footer.query_one(Countdown).render() == ""
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
        assert app.screen.query_one(StatusFooter).refreshing is True


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
            screen.set_database("MYDB")
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
    from snowcone.service.jobs import Priority

    put_schemas(repo, "MYDB", "AAA")
    config.set(CACHE_TTL, 60)
    try:
        fetch = RecordingFetch()
        app = Harness(repo, fetch)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.screen.set_database("MYDB")
            await pilot.pause()
            fetch.requests.clear()
            age_by(repo, schemas_scope("MYDB"), 120)
            app.screen._sweep()
            await pilot.pause()
        assert dict(fetch.requests)["schemas:MYDB"] == int(Priority.BACKGROUND)
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
            app.screen.set_database("MYDB")
            await pilot.pause()
            fetch.requests.clear()
            app.screen._sweep()
            await pilot.pause()
        assert fetch.scopes == []
    finally:
        config.set(CACHE_TTL, CACHE_TTL.default)
