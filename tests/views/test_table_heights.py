# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Every detail table fits its pane and scrolls, however many rows it holds."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual.widgets import DataTable, TabbedContent

from nivaria.cache.repository import Repository
from nivaria.models import ObjectType, SchemaRef, objects_scope
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import Harness, load, put_tag

ROWS = 60


def _objects(object_type: ObjectType) -> Callable[[Repository], None]:
    def seed(repo: Repository) -> None:
        load(
            repo,
            objects_scope(object_type, "MYDB", "PUBLIC"),
            [
                {
                    "database_name": "MYDB",
                    "schema_name": "PUBLIC",
                    "name": f"OBJECT_{i:03}",
                    "kind": "TABLE",
                    "comment": "",
                }
                for i in range(ROWS)
            ],
        )

    return seed


def _tags(schema: str | None) -> Callable[[Repository], None]:
    def seed(repo: Repository) -> None:
        for i in range(ROWS):
            put_tag(repo, "MYDB", schema, TAG_NAME=f"TAG_{i:03}")

    return seed


def _category(object_type: ObjectType) -> Callable[[DetailPane], None]:
    return lambda pane: pane.show_category(objects_scope(object_type, "MYDB", "PUBLIC"))


@pytest.mark.parametrize(
    ("seed", "show", "tab", "table_id"),
    [
        pytest.param(
            _objects(ObjectType.PIPE),
            _category(ObjectType.PIPE),
            "tab-activity",
            "detail-list",
            id="pipes-activity",
        ),
        pytest.param(
            _objects(ObjectType.PIPE),
            _category(ObjectType.PIPE),
            "tab-status",
            "detail-status",
            id="pipes-status",
        ),
        pytest.param(
            _tags("PUBLIC"),
            lambda pane: pane.show_schema(SchemaRef("MYDB", "PUBLIC")),
            "tab-tags",
            "detail-tags",
            id="schema-tags",
        ),
        pytest.param(
            _tags(None),
            lambda pane: pane.show_database("MYDB"),
            "tab-tags",
            "detail-tags",
            id="database-tags",
        ),
        pytest.param(
            _objects(ObjectType.TABLE),
            _category(ObjectType.TABLE),
            None,
            "detail-list",
            id="tables-untabbed",
        ),
    ],
)
async def test_every_detail_table_scrolls_within_its_pane_instead_of_growing_past_it(
    repo, seed, show, tab, table_id
) -> None:
    """A table left to Textual's `height: auto` grows to all of its rows: in
    a tab nothing caps it, and outside one the notes above push its last rows
    off the bottom. Either way the pane clips it, with nothing to scroll."""
    seed(repo)
    app = Harness(repo)
    async with app.run_test(size=(100, 20)) as pilot:
        pane = app.query_one(DetailPane)
        show(pane)
        await pilot.pause()
        if tab is not None:
            pane.query_one(TabbedContent).active = tab
        await pilot.pause()
        await pilot.pause()
        view = pane.view
        assert view is not None
        table = view.query_one(f"#{table_id}", DataTable)
        assert table.region.bottom <= view.region.bottom
        assert table.max_scroll_y > 0
