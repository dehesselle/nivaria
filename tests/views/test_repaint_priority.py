# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""A repaint asks for what else its view needs at the priority of the fetch
it answers, so a sweep's result never asks for more than the sweep would."""

from __future__ import annotations

import pytest

from nivaria.models import column_tag_references_scope, columns_scope
from nivaria.service.jobs import Priority
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import Harness, put_columns, put_table


@pytest.mark.parametrize(
    "landed", [Priority.BACKGROUND, Priority.VISIBLE, Priority.INTERACTIVE]
)
async def test_a_repaint_asks_at_the_priority_of_the_fetch_that_landed(
    repo, landed: Priority
) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        put_columns(repo, "ORDERS", ("ID", "NUMBER"))
        app.priorities.clear()
        pane.cache_updated(columns_scope("MYDB", "PUBLIC", "ORDERS"), landed)
        await pilot.pause()
    tags = column_tag_references_scope("MYDB", "PUBLIC", "ORDERS").key
    assert app.priorities[tags] is landed


async def test_what_the_user_does_after_a_background_repaint_is_interactive(
    repo,
) -> None:
    """The repaint's priority is its own, not left behind for the next ask."""
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view = pane.show(ref)
        await pilot.pause()
        put_columns(repo, "ORDERS", ("ID", "NUMBER"))
        pane.cache_updated(
            columns_scope("MYDB", "PUBLIC", "ORDERS"), Priority.BACKGROUND
        )
        await pilot.pause()
        app.priorities.clear()
        view.ensure_all()
        await pilot.pause()
    tags = column_tag_references_scope("MYDB", "PUBLIC", "ORDERS").key
    assert app.priorities[tags] is Priority.INTERACTIVE
