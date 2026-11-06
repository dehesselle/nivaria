# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""A SQL tab follows the SHOW row it reads, which has no scope of its own."""

from __future__ import annotations

import pytest
from textual.widgets import TextArea

from nivaria.models import ObjectType, objects_scope
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import Harness, put_pipe, put_task, put_view


@pytest.mark.parametrize(
    ("object_type", "put", "field"),
    [
        (ObjectType.PIPE, put_pipe, "definition"),
        (ObjectType.TASK, put_task, "definition"),
        (ObjectType.VIEW, put_view, "text"),
    ],
)
async def test_a_sql_tab_repaints_when_the_listing_lands(
    repo, object_type: ObjectType, put, field: str
) -> None:
    """F5 asks for the listing, and without this the new text would land in
    the cache and stay off screen until something else repainted the view."""
    ref = put(repo, "THING", **{field: "SELECT 'old'"})
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert pane.query_one("#detail-sql", TextArea).text == "SELECT 'old'"

        put(repo, "THING", **{field: "SELECT 'new'"})
        pane.cache_updated(objects_scope(object_type, "MYDB", "PUBLIC"))
        await pilot.pause()
        assert pane.query_one("#detail-sql", TextArea).text == "SELECT 'new'"
