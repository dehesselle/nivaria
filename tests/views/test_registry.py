# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The detail pane's view registries, and the swap rule."""

from __future__ import annotations

import pytest
from textual.widgets import Static

from nivaria.models import (
    ObjectRef,
    ObjectType,
    Scope,
    ScopeType,
)
from nivaria.ui.views.base import registered_types, view_for
from nivaria.ui.views.pipe_view import PipeView
from nivaria.ui.views.table_view import TableView
from nivaria.ui.views.task_view import TaskView
from nivaria.ui.views.view_view import ViewView
from nivaria.ui.widgets.detail_pane import DetailPane
from tests.views.harness import (
    Harness,
    put_columns,
    put_pipe,
    put_table,
)

# ---------------------------------------------------------------- registry


def test_every_kind_the_tree_shows_has_a_view_or_is_known_missing() -> None:
    """A type with no view is a placeholder, not a crash -- but the ones that
    do have views must actually be registered by importing their modules."""
    assert view_for(ObjectType.TABLE) is TableView
    assert view_for(ObjectType.VIEW) is ViewView
    assert view_for(ObjectType.PIPE) is PipeView
    assert view_for(ObjectType.TASK) is TaskView


def test_registration_is_by_object_kind() -> None:
    assert {
        ObjectType.TABLE,
        ObjectType.VIEW,
        ObjectType.PIPE,
        ObjectType.TASK,
    } <= registered_types()


def test_registering_a_duplicate_kind_is_refused() -> None:
    """Two views for one type means one silently wins; better to fail loudly
    at import time than to wonder which is showing."""
    from nivaria.ui.views.base import ObjectView, register_view

    with pytest.raises(ValueError):

        @register_view
        class Duplicate(ObjectView):
            OBJECT_TYPE = ObjectType.TABLE

            def update_content(self) -> None:
                pass


# ------------------------------------------------------------------ swapping


async def test_showing_a_table_mounts_the_table_view(repo) -> None:
    ref = put_table(repo, "ORDERS")
    put_columns(repo, "ORDERS", ("ID", "NUMBER"), ("TOTAL", "NUMBER"))
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        assert isinstance(pane.view, TableView)


async def test_moving_between_two_tables_reuses_the_view(repo) -> None:
    """The reason the pane swaps on type rather than on object: arrowing down
    three hundred tables must not remount a widget three hundred times."""
    first = put_table(repo, "ORDERS")
    second = put_table(repo, "CUSTOMERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        view_a = pane.show(first)
        await pilot.pause()
        view_b = pane.show(second)
        await pilot.pause()
        assert view_a is view_b
        assert pane.view.ref.name == "CUSTOMERS"


async def test_moving_to_a_different_kind_replaces_the_view(repo) -> None:
    table = put_table(repo, "ORDERS")
    pipe = put_pipe(repo, "LOAD_ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(table)
        await pilot.pause()
        pane.show(pipe)
        await pilot.pause()
        assert isinstance(pane.view, PipeView)
        assert len(pane.query(TableView)) == 0


async def test_an_unregistered_kind_shows_a_placeholder(repo) -> None:
    """A type the tree can produce but nothing has a view for yet must not
    take the app down."""
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        result = pane.show(ObjectRef(ObjectType.STAGE, "MYDB", "PUBLIC", "S1"))
        await pilot.pause()
        assert result is None
        assert pane.query_one(".detail-placeholder", Static)


async def test_a_second_object_without_a_view_replaces_the_placeholder(repo) -> None:
    """The outgoing placeholder is still attached when the next one mounts, so
    two view-less selections in a row must not collide."""
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ObjectRef(ObjectType.STAGE, "MYDB", "PUBLIC", "S1"))
        await pilot.pause()
        pane.show(ObjectRef(ObjectType.STAGE, "MYDB", "PUBLIC", "S2"))
        await pilot.pause()
        pane.show_category(Scope(ScopeType.OBJECTS, "MYDB", "PUBLIC"))
        await pilot.pause()
        placeholders = pane.query(".detail-placeholder").results(Static)
        assert [str(p.content) for p in placeholders] == ["No list view for this yet."]


async def test_clearing_empties_the_pane(repo) -> None:
    ref = put_table(repo, "ORDERS")
    app = Harness(repo)
    async with app.run_test(size=(100, 30)) as pilot:
        pane = app.query_one(DetailPane)
        pane.show(ref)
        await pilot.pause()
        pane.clear()
        await pilot.pause()
        assert pane.view is None
        assert pane.scopes() == []
