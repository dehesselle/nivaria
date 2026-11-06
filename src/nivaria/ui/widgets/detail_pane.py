# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The container that mounts one detail view at a time.

The swap rule is the whole point: if the object being shown is of the same type
as the one already mounted, the view is re-pointed rather than remounted. That
is what makes arrowing down a list of three hundred tables cheap -- the widget,
its columns and its scroll position all survive, and there is no flicker.
A different type is a genuine change of shape, so that view is replaced.

The pane also holds back what a view asks of Snowflake until the tree cursor
has rested on it (`CURSOR_SETTLE_MS`). A view still paints what is cached at
once. Deciding only after the wait, rather than deciding at paint time and
holding the requests, leaves no list of them to keep up to date, and the
answer reflects the cache when the cursor stopped.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.timer import Timer
from textual.widgets import Static

from nivaria.cache.repository import Repository
from nivaria.models import ObjectRef, SchemaRef, Scope
from nivaria.settings import CACHE_TTL, CURSOR_SETTLE_MS, config
from nivaria.ui.views.base import (
    CategoryView,
    ObjectView,
    _ScopedView,
    category_view_for,
    view_for,
)
from nivaria.ui.views.schema_view import DatabaseView, SchemaView


class DetailPane(Vertical):
    """Shows whatever object the tree or the search modal last selected."""

    def __init__(self, repo: Repository, **kwargs) -> None:
        super().__init__(**kwargs)
        self.repo = repo
        self._view: ObjectView | CategoryView | SchemaView | DatabaseView | None = None
        self._ref: ObjectRef | None = None
        self._scope: Scope | None = None
        self._schema: SchemaRef | None = None
        self._database: str | None = None
        self._settle_timer: Timer | None = None

    @property
    def view(self) -> ObjectView | CategoryView | SchemaView | DatabaseView | None:
        return self._view

    @property
    def ref(self) -> ObjectRef | None:
        return self._ref

    @property
    def scope(self) -> Scope | None:
        return self._scope

    def show(self, ref: ObjectRef) -> ObjectView | None:
        """Point the pane at an object. Returns the view now showing it.

        Pointing it at what it already shows changes nothing: re-showing
        would repaint the view and reset its sort and cursor, though the user
        never moved. `clear()` forgets what was shown, so an emptied pane is
        never mistaken for this case.
        """
        if ref == self._ref and isinstance(self._view, ObjectView):
            return self._view
        self._ref = ref
        self._scope = None
        self._schema = None
        self._database = None
        view_class = view_for(ref.type)
        if view_class is None:
            self._show_placeholder(f"No detail view for {ref.type.value} yet.")
            return None

        current = self._view
        if current is not None and type(current) is view_class:
            self._wait_for_cursor(current)
            current.show(ref)
            return current

        self.remove_children()
        view = view_class(self.repo, ref)
        self._view = view
        self._wait_for_cursor(view)
        self.mount(view)
        return view

    def show_category(self, scope: Scope) -> CategoryView | None:
        """Point the pane at a category node ("Tables (n)", ...).

        Same swap rule as `show`: same type re-points the mounted view, a
        different type (or one with no registered view) replaces it. Like
        `show`, pointing it at what it already shows changes nothing.
        """
        if scope == self._scope and isinstance(self._view, CategoryView):
            return self._view
        self._ref = None
        self._scope = scope
        self._schema = None
        self._database = None
        object_type = scope.object_type
        view_class = category_view_for(object_type) if object_type is not None else None
        if view_class is None:
            label = object_type.plural if object_type is not None else "this"
            self._show_placeholder(f"No list view for {label} yet.")
            return None

        current = self._view
        if current is not None and type(current) is view_class:
            self._wait_for_cursor(current)
            current.show(scope)
            return current

        self.remove_children()
        view = view_class(self.repo, scope)
        self._view = view
        self._wait_for_cursor(view)
        self.mount(view)
        return view

    def show_schema(self, ref: SchemaRef) -> SchemaView:
        """Point the pane at a schema's Grants/Tags view.

        No registry lookup, unlike `show`/`show_category`: there is exactly
        one view for a schema node, not one of several types sharing a
        lookup, so this constructs SchemaView directly. Like `show`,
        pointing it at what it already shows changes nothing.
        """
        if ref == self._schema and type(self._view) is SchemaView:
            return self._view
        self._ref = None
        self._scope = None
        self._database = None
        self._schema = ref

        current = self._view
        if type(current) is SchemaView:
            self._wait_for_cursor(current)
            current.show(ref)
            return current

        self.remove_children()
        view = SchemaView(self.repo, ref)
        self._view = view
        self._wait_for_cursor(view)
        self.mount(view)
        return view

    def show_database(self, database: str) -> DatabaseView:
        """Point the pane at a database's Grants/Tags view. See `show_schema`."""
        if database == self._database and type(self._view) is DatabaseView:
            return self._view
        self._ref = None
        self._scope = None
        self._schema = None
        self._database = database

        current = self._view
        if type(current) is DatabaseView:
            self._wait_for_cursor(current)
            current.show(database)
            return current

        self.remove_children()
        view = DatabaseView(self.repo, database)
        self._view = view
        self._wait_for_cursor(view)
        self.mount(view)
        return view

    def clear(self) -> None:
        self._view = None
        self._ref = None
        self._scope = None
        self._schema = None
        self._database = None
        self.remove_children()

    def set_repo(self, repo: Repository) -> None:
        """Point at a different cache entirely -- see `ObjectTree.set_repo`.

        The mounted view (if any) is bound to the old repo internally, so it
        is discarded rather than re-pointed; `show()` builds a fresh one
        against the new repo the next time something is selected.
        """
        self.repo = repo
        self.clear()

    def cache_updated(self, scope: Scope) -> None:
        if self._view is not None:
            self._view.cache_updated(scope)

    def scopes(self) -> list[Scope]:
        """What the pane is reading."""
        return self._view.scopes() if self._view is not None else []

    def watched_scopes(self) -> list[Scope]:
        """What the staleness sweep may refresh: nothing until the cursor has
        rested. A scope never fetched is due at once, so the footer's tick
        would otherwise run a query for whichever row the cursor is passing.
        """
        return self.scopes() if self._view is not None and self._view.settled else []

    def _wait_for_cursor(self, view: _ScopedView) -> None:
        """Hold back what `view` asks for until the cursor has rested on it."""
        if self._settle_timer is not None:
            self._settle_timer.stop()
            self._settle_timer = None
        # Read here rather than at construction, like every setting, so a
        # change applies to the next selection.
        delay = config.get(CURSOR_SETTLE_MS)
        view.settled = delay <= 0
        if not view.settled:
            self._settle_timer = self.set_timer(delay / 1000, self._cursor_rested)

    def _cursor_rested(self) -> None:
        """Ask what the view on screen needs, now that the cursor has stopped.

        A view not mounted yet asks when it first paints. One that is asks
        from its own message pump, not from this timer's: Textual stops a
        message from bubbling past its sender, so a `ScopeNeeded` sent from
        the pane's pump would never leave the pane.
        """
        self._settle_timer = None
        view = self._view
        if view is None:
            return
        view.settled = True
        if view.is_mounted:
            view.call_later(view.ensure_all, config.get(CACHE_TTL))

    def _show_placeholder(self, message: str) -> None:
        self._view = None
        self.remove_children()
        # A class, not an id: removal is deferred, so the outgoing placeholder
        # is still attached when this one mounts and a fixed id would collide.
        self.mount(Static(message, classes="detail-placeholder"))
