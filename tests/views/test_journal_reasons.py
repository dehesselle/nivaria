# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""What a detail view tells the journal: the trigger of each request, and
what the user did to cause it."""

from __future__ import annotations

import pytest
from textual.widgets import TabbedContent

from nivaria.models import (
    column_tag_references_scope,
    columns_scope,
    object_grants_scope,
)
from nivaria.settings import CURSOR_SETTLE_MS, JOURNAL_VERBOSE, config
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import Harness, load, put_columns, put_table

NEVER = 60_000


def journal_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "nivaria.journal"]


@pytest.fixture
def waiting() -> None:
    config.set(CURSOR_SETTLE_MS, NEVER)


@pytest.mark.usefixtures("waiting")
async def test_a_rested_cursor_asks_as_cursor_settled_and_says_where_it_is(
    repo, caplog
) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert app.asked == []
        pane._cursor_rested()
        await pilot.pause()
    assert (
        columns_scope("MYDB", "PUBLIC", "ORDERS").key,
        "cursor settled",
        "never fetched",
    ) in app.asked
    assert "user  cursor rested on table MYDB.PUBLIC.ORDERS" in journal_lines(caplog)


async def test_an_opened_tab_asks_as_tab_opened_and_is_journalled(repo, caplog) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(DetailPane).show(ref)
        await pilot.pause()
        caplog.clear()
        view.query_one(TabbedContent).active = "tab-grants"
        await pilot.pause()
    assert (object_grants_scope(ref).key, "tab opened", "never fetched") in app.asked
    assert "user  tab Grants on table MYDB.PUBLIC.ORDERS" in journal_lines(caplog)


async def test_a_view_being_shown_journals_no_tab_of_its_own(repo, caplog) -> None:
    """Only a tab the user opened is a user line, not the first one a view
    mounts with."""
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
        await pilot.pause()
    assert not any(line.startswith("user  tab") for line in journal_lines(caplog))


async def test_a_repaint_answering_an_update_asks_as_repaint(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(DetailPane).show(ref)
        await pilot.pause()
        put_columns(repo, "ORDERS", ("ID", "NUMBER"))
        app.asked.clear()
        view.cache_updated(columns_scope("MYDB", "PUBLIC", "ORDERS"))
        await pilot.pause()
    assert (
        column_tag_references_scope("MYDB", "PUBLIC", "ORDERS").key,
        "repaint",
        "never fetched",
    ) in app.asked


async def test_a_fresh_scope_at_rest_says_why_nothing_was_fetched_when_verbose(
    repo, caplog
) -> None:
    config.set(JOURNAL_VERBOSE, True)
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DetailPane).show(ref)
        await pilot.pause()
    assert any(
        line.startswith("skip  columns:MYDB.PUBLIC.ORDERS  cursor settled: fresh, age ")
        for line in journal_lines(caplog)
    )


async def test_a_fresh_scope_in_a_repaint_is_not_journalled(repo, caplog) -> None:
    """Every landing page would otherwise add a line per scope on screen."""
    config.set(JOURNAL_VERBOSE, True)
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"))
    load(repo, column_tag_references_scope("MYDB", "PUBLIC", "ORDERS"), [])
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(DetailPane).show(ref)
        await pilot.pause()
        caplog.clear()
        view.cache_updated(columns_scope("MYDB", "PUBLIC", "ORDERS"))
        await pilot.pause()
    assert not any(line.startswith("skip") for line in journal_lines(caplog))
