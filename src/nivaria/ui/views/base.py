# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Detail views, one per object type, and the registry that finds them.

Why a registry and not a `ContentSwitcher`: a switcher needs every child
mounted up front. With a growing set of object types that means a dozen widget
trees resident at all times, each needing a coherent "nothing selected" state,
each subscribing to cache updates it does not care about. Mounting on demand
keeps exactly one view alive -- the one being looked at.

A view owns its whole body: there is one registry, one contract
(`compose()`/`update_content()`), not a second one for a separate context
strip. Whatever a view wants to show -- including its own identity or status
-- is content it composes itself, wherever in its own body it belongs.

Registration is a module-level dict populated by a decorator at import time.
No entry points, nothing to configure -- adding a type is one new module plus
one import.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

from tcoda.widgets import TabbedContent, TabPane
from textual.app import ComposeResult
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, TextArea
from textual.widgets.data_table import CellDoesNotExist

from nivaria.cache.repository import Repository
from nivaria.models import (
    CacheState,
    ObjectRef,
    ObjectType,
    Scope,
    is_stale,
    objects_scope,
)
from nivaria.settings import CACHE_TTL, config
from nivaria.ui.messages import ObjectOpened, ScopeNeeded

#: A single object's defining SQL.
SQL = "detail-sql"

_registry: dict[ObjectType, type[ObjectView]] = {}
_category_registry: dict[ObjectType, type[CategoryView]] = {}


class _ScopedView(Widget):
    """What `ObjectView` and `CategoryView` share: repainting from the cache
    and asking for what is missing or stale.

    Split out because a category view (keyed by a `Scope`, no single object)
    is not a rarer kind of `ObjectView` -- it has no `ref` and no `fqn` -- but
    it needs the exact same freshness and cache-update plumbing.
    """

    #: The scope whose cache update this repaint answers, while it runs.
    _announcing: Scope | None = None

    #: Whether the cursor has rested here long enough for `ensure` to ask
    #: anything. `DetailPane` manages it; a view outside one is always settled.
    settled: bool = True

    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self.repo = repo

    def on_mount(self) -> None:
        self.repaint()

    def cache_updated(self, scope: Scope) -> None:
        """Repaint if the scope that changed is one this view is showing.

        Not before it has mounted: its widgets do not exist yet, and the paint
        `on_mount` makes reads what this update announced anyway.
        """
        if self.is_mounted and scope in self.scopes():
            self._announcing = scope
            try:
                self.repaint()
            finally:
                self._announcing = None

    def repaint(self) -> None:
        """Ask for whatever is missing or stale, and paint what is cached."""
        self.ensure_all()
        self.update_content()

    def scopes(self) -> list[Scope]:
        """Cache scopes this view reads, for freshness and the countdown."""
        return []

    @abstractmethod
    def update_content(self) -> None:
        """Paint from the cache, asking for nothing: see `repaint`."""

    def _tab_shown(self, tab: str) -> bool:
        """Whether `tab` is up, for a scope fetched only while its tab is."""
        return self.is_mounted and self.query_one(TabbedContent).active == tab

    @contextmanager
    def keeping_cursor(self, table: DataTable) -> Iterator[None]:
        """Repopulate `table` inside this block without losing the user's row.

        `DataTable.clear()` puts the cursor back on the first row, so every
        cache update landing on screen would throw away whatever row the
        user was reading -- a column's tags, a load's error. Only a repaint
        answering a cache update keeps the row: that is the same object read
        again. Shown afresh (mounted, or pointed at another object), the
        table starts at the top as before.

        The row is found again by key, else by position: a row that went
        away leaves the cursor on whatever took its place, and a table
        without stable keys keeps at least its position.
        """
        if self._announcing is None:
            yield
            return
        row = table.cursor_row
        try:
            key = table.coordinate_to_cell_key(Coordinate(row, 0)).row_key
        except CellDoesNotExist:
            key = None
        yield
        if not table.row_count:
            return
        if key is not None and key in table.rows:
            row = table.get_row_index(key)
        table.move_cursor(row=min(row, table.row_count - 1), animate=False)

    # ------------------------------------------------------------ freshness

    def ensure(self, scope: Scope) -> None:
        """Ask for `scope` if it is missing or stale.

        Never for the scope whose own update is being painted, nor, from any
        repaint answering a cache update, for one that failed. A failed scope
        keeps the `fetched_at` of its last good pass, if it had one, and so
        reads as stale. Asking from those repaints made a failing statement
        retry itself, and made two failing scopes on one view retry each other
        for as long as the view stayed on screen. The sweep, navigating back
        and a warehouse switch still retry it.

        Nor while the cursor is still moving: `DetailPane` calls this for
        every scope once it has rested, so the answer reflects the cache then
        rather than when the cursor passed.
        """
        if not self.settled or scope == self._announcing:
            return
        entry = self.repo.entry(scope)
        if self._announcing is not None and entry.state is CacheState.ERROR:
            return
        if is_stale(entry.age(), config.get(CACHE_TTL)):
            self.post_message(
                ScopeNeeded(
                    scope,
                    interactive=True,
                    from_update=self._announcing is not None,
                )
            )

    def ensure_all(self) -> None:
        """Ask for whatever this view reads that is missing or stale.

        `scopes()` holds the conditions a view asks under (tab shown, target
        table resolved), so no second list of them can drift.
        """
        for scope in self.scopes():
            self.ensure(scope)


class ObjectView(_ScopedView):
    """One object type's detail UI, mounted into the detail pane on demand."""

    OBJECT_TYPE: ClassVar[ObjectType]

    def __init__(self, repo: Repository, ref: ObjectRef) -> None:
        super().__init__(repo)
        self.ref = ref

    def listing_scope(self) -> Scope:
        """The list holding this object's SHOW row, for a view that reads it."""
        return objects_scope(self.ref.type, self.ref.database, self.ref.schema)

    def compose_sql_tab(self) -> ComposeResult:
        """The tab of a type with a defining statement. See `paint_sql`."""
        with TabPane("SQL", mnemonic="s", id="tab-sql"):
            yield TextArea(
                "",
                id=SQL,
                read_only=True,
                soft_wrap=True,
                show_line_numbers=False,
            )

    def paint_sql(self, column: str) -> None:
        """Show the statement `column` holds in this object's SHOW row, which
        has no scope of its own: the listing's update repaints it."""
        row = self.repo.object_detail(self.ref)
        text = (row or {}).get(column) or "-- not cached"
        self.query_one(f"#{SQL}", TextArea).text = text

    def show(self, ref: ObjectRef) -> None:
        """Re-point an already mounted view at another object of its type.

        This is the path that makes moving between two tables cheap: the
        widget, its columns and its scroll position all survive, so there is
        no remount and no flicker.
        """
        self.ref = ref
        if self.is_mounted:
            self.repaint()


class CategoryView(_ScopedView):
    """One category's list UI (e.g. every table in a schema), mounted into
    the detail pane on demand -- the group-node counterpart to `ObjectView`.
    """

    OBJECT_TYPE: ClassVar[ObjectType]

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo)
        self.scope = scope
        #: This paint's rows, keyed by object name: the merged row a hover
        #: tooltip reads its full-precision value from (`row_for`).
        self._rows: dict[str, dict[str, Any]] = {}

    def show(self, scope: Scope) -> None:
        """Re-point an already mounted view at another category of the same
        type -- e.g. moving to another schema's Tables node."""
        self.scope = scope
        if self.is_mounted:
            self.repaint()

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that object's detail view, revealed in the tree.

        Takes the same path a search hit does (`MainScreen._select` ->
        `tree.reveal` + `detail.show`), so the tree cursor follows to the
        object instead of staying on the category, which would leave nothing
        on screen naming what the Detail pane shows. Its own message rather
        than the tree's `ObjectSelected`, which only shows: a tree highlight
        must never reveal (docs/DESIGN.md "A highlight is not a jump").
        """
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(self.OBJECT_TYPE, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectOpened(ref))


def register_view[T: type[ObjectView]](cls: T) -> T:
    """Register a view for its OBJECT_TYPE."""
    object_type = cls.OBJECT_TYPE
    if object_type in _registry:
        raise ValueError(f"a view is already registered for {object_type}")
    _registry[object_type] = cls
    return cls


def view_for(object_type: ObjectType) -> type[ObjectView] | None:
    return _registry.get(object_type)


def registered_types() -> frozenset[ObjectType]:
    return frozenset(_registry)


def register_category_view[T: type[CategoryView]](cls: T) -> T:
    """Register a category (group-node) view for its OBJECT_TYPE."""
    object_type = cls.OBJECT_TYPE
    if object_type in _category_registry:
        raise ValueError(f"a category view is already registered for {object_type}")
    _category_registry[object_type] = cls
    return cls


def category_view_for(object_type: ObjectType) -> type[CategoryView] | None:
    return _category_registry.get(object_type)


def registered_category_types() -> frozenset[ObjectType]:
    return frozenset(_category_registry)
