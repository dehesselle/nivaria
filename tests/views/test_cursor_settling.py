# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The detail pane asks for nothing until the tree cursor has rested.

A delay long enough never to fire lets a test call `_cursor_rested` itself, so
nothing here depends on how fast the machine running it is.
"""

from __future__ import annotations

import time

import pytest
from textual.widgets import DataTable, TabbedContent

from nivaria.cache.repository import Repository
from nivaria.models import (
    ObjectType,
    column_tag_references_scope,
    columns_scope,
    object_grants_scope,
    objects_scope,
)
from nivaria.settings import CACHE_TTL, CURSOR_SETTLE_MS, config
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import Harness, load, put_columns, put_table

NEVER = 60_000


@pytest.fixture
def waiting() -> None:
    config.set(CURSOR_SETTLE_MS, NEVER)


def _fetched_long_ago(repo: Repository, scope) -> None:
    generation = repo.begin_pass(scope, time.time() - 10 * config.get(CACHE_TTL))
    repo.write_page(scope, generation, [])
    repo.finish_pass(scope, generation)


@pytest.mark.usefixtures("waiting")
async def test_a_view_paints_the_cache_at_once_and_asks_for_nothing(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-columns", DataTable).row_count == 1
    assert app.needed == []


@pytest.mark.usefixtures("waiting")
async def test_a_never_fetched_scope_is_asked_for_once_the_cursor_rests(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        pane._cursor_rested()
        await pilot.pause()
    assert columns_scope("MYDB", "PUBLIC", "ORDERS").key in app.needed


@pytest.mark.usefixtures("waiting")
async def test_an_expired_scope_is_asked_for_once_the_cursor_rests(repo) -> None:
    ref = put_table(repo, "ORDERS")
    _fetched_long_ago(repo, columns_scope("MYDB", "PUBLIC", "ORDERS"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        pane._cursor_rested()
        await pilot.pause()
    assert columns_scope("MYDB", "PUBLIC", "ORDERS").key in app.needed


@pytest.mark.usefixtures("waiting")
async def test_a_fresh_view_asks_for_nothing_when_the_cursor_rests(repo) -> None:
    ref = put_table(repo, "ORDERS")
    load(repo, columns_scope("MYDB", "PUBLIC", "ORDERS"), [])
    load(repo, column_tag_references_scope("MYDB", "PUBLIC", "ORDERS"), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        pane._cursor_rested()
        await pilot.pause()
    assert app.needed == []


@pytest.mark.usefixtures("waiting")
async def test_only_the_object_the_cursor_rested_on_is_asked_for(repo) -> None:
    passed = put_table(repo, "PASSED")
    landed = put_table(repo, "LANDED")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(passed)
        await pilot.pause()
        pane.show(landed)
        await pilot.pause()
        pane._cursor_rested()
        await pilot.pause()
    assert columns_scope("MYDB", "PUBLIC", "LANDED").key in app.needed
    assert columns_scope("MYDB", "PUBLIC", "PASSED").key not in app.needed


@pytest.mark.usefixtures("waiting")
async def test_a_tab_opened_during_the_wait_is_asked_for_when_it_ends(repo) -> None:
    ref = put_table(repo, "ORDERS")
    grants = object_grants_scope(ref).key
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        view.query_one(TabbedContent).active = "tab-grants"
        await pilot.pause()
        assert grants not in app.needed
        pane._cursor_rested()
        await pilot.pause()
    assert grants in app.needed


@pytest.mark.usefixtures("waiting")
async def test_a_tab_opened_after_the_wait_is_asked_for_at_once(repo) -> None:
    """The wait is about the cursor moving through the tree, not the tabs."""
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        pane._cursor_rested()
        await pilot.pause()
        view.query_one(TabbedContent).active = "tab-grants"
        await pilot.pause()
    assert object_grants_scope(ref).key in app.needed


@pytest.mark.usefixtures("waiting")
async def test_the_sweep_sees_a_view_only_once_the_cursor_has_rested(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.watched_scopes() == []
        pane._cursor_rested()
        assert (
            objects_scope(ObjectType.TABLE, "MYDB", "PUBLIC") in pane.watched_scopes()
        )


async def test_the_wait_ends_by_itself(repo) -> None:
    config.set(CURSOR_SETTLE_MS, 10)
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause(0.2)
    assert columns_scope("MYDB", "PUBLIC", "ORDERS").key in app.needed


async def test_no_delay_asks_at_once(repo) -> None:
    config.set(CURSOR_SETTLE_MS, 0)
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
    assert columns_scope("MYDB", "PUBLIC", "ORDERS").key in app.needed
