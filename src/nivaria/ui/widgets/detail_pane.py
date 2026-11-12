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

from weakref import WeakSet

from rich.text import Text
from tcoda.widgets import TabbedContent
from textual.containers import Vertical
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static

from nivaria import journal
from nivaria.cache.repository import Repository
from nivaria.journal import Reason
from nivaria.models import ObjectRef, SchemaRef, Scope
from nivaria.service.jobs import Priority
from nivaria.settings import CURSOR_SETTLE_MS, config
from nivaria.ui.views.base import _ScopedView, category_view_for, view_for
from nivaria.ui.views.schema_view import DatabaseView, SchemaView


class DetailPane(Vertical):
    """Shows whatever object the tree or the search modal last selected."""

    def __init__(self, repo: Repository, **kwargs) -> None:
        super().__init__(**kwargs)
        self.repo = repo
        self._view: _ScopedView | None = None
        #: What the pane was last pointed at: an object, a category's scope, a
        #: schema or a database.
        self._shown: ObjectRef | Scope | SchemaRef | str | None = None
        self._settle_timer: Timer | None = None
        #: Tab bars that have activated their first tab. Weak, because views
        #: come and go with the cursor.
        self._tabs_seen: WeakSet[TabbedContent] = WeakSet()

    @property
    def view(self) -> _ScopedView | None:
        return self._view

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        # Here rather than in each view, so every view's tabs are journalled
        # the same way. Not stopped: the views' own handlers have seen it.
        # The first activation of each tab bar is its mount, not the user.
        if event.tabbed_content not in self._tabs_seen:
            self._tabs_seen.add(event.tabbed_content)
            return
        if self._shown is None:
            return
        label = event.tab.label
        name = label.plain if isinstance(label, Content | Text) else str(label)
        journal.user("tab {} on {}", name, journal.subject(self._shown))

    @property
    def ref(self) -> ObjectRef | None:
        return self._shown if isinstance(self._shown, ObjectRef) else None

    @property
    def scope(self) -> Scope | None:
        return self._shown if isinstance(self._shown, Scope) else None

    def show(self, ref: ObjectRef) -> _ScopedView | None:
        """Point the pane at an object. Returns the view now showing it."""
        view_class = view_for(ref.type)
        if view_class is None:
            self._shown = ref
            self._show_placeholder(f"No detail view for {ref.type.value} yet.")
            return None
        return self._point_at(ref, view_class)

    def show_category(self, scope: Scope) -> _ScopedView | None:
        """Point the pane at a category node ("Tables (n)", ...)."""
        object_type = scope.object_type
        view_class = category_view_for(object_type) if object_type is not None else None
        if view_class is None:
            self._shown = scope
            label = object_type.plural if object_type is not None else "this"
            self._show_placeholder(f"No list view for {label} yet.")
            return None
        return self._point_at(scope, view_class)

    def show_schema(self, ref: SchemaRef) -> _ScopedView:
        """Point the pane at a schema's Grants/Tags view.

        No registry lookup, unlike `show`/`show_category`: there is exactly
        one view for a schema node, not one of several types sharing a lookup.
        """
        return self._point_at(ref, SchemaView)

    def show_database(self, database: str) -> _ScopedView:
        """Point the pane at a database's Grants/Tags view. See `show_schema`."""
        return self._point_at(database, DatabaseView)

    def _point_at(
        self, target: ObjectRef | Scope | SchemaRef | str, view_class: type[_ScopedView]
    ) -> _ScopedView:
        """The swap rule: the same type re-points the mounted view, a different
        one replaces it.

        Pointing it at what it already shows changes nothing: re-showing would
        repaint the view and reset its sort and cursor, though the user never
        moved. `clear()` forgets what was shown, so an emptied pane is never
        mistaken for this case.
        """
        if target == self._shown and self._view is not None:
            return self._view
        self._shown = target

        current = self._view
        if type(current) is view_class:
            self._wait_for_cursor(current)
            current.show(target)
            return current

        self.remove_children()
        view = view_class(self.repo, target)
        self._view = view
        self._wait_for_cursor(view)
        self.mount(view)
        return view

    def clear(self) -> None:
        self._view = None
        self._shown = None
        self.remove_children()

    def set_repo(self, repo: Repository) -> None:
        """Point at a different cache entirely -- see `ObjectTree.set_repo`.

        The mounted view (if any) is bound to the old repo internally, so it
        is discarded rather than re-pointed; `show()` builds a fresh one
        against the new repo the next time something is selected.
        """
        self.repo = repo
        self.clear()

    def cache_updated(
        self, scope: Scope, priority: Priority = Priority.INTERACTIVE
    ) -> None:
        if self._view is not None:
            self._view.cache_updated(scope, priority)

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
        elif self._shown is not None:
            journal.user("cursor rested on {}", journal.subject(self._shown))

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
        if self._shown is not None:
            journal.user("cursor rested on {}", journal.subject(self._shown))
        if view.is_mounted:
            view.call_later(view.ensure_all, Reason.CURSOR_SETTLED)

    def _show_placeholder(self, message: str) -> None:
        self._view = None
        self.remove_children()
        # A class, not an id: removal is deferred, so the outgoing placeholder
        # is still attached when this one mounts and a fixed id would collide.
        self.mount(Static(message, classes="detail-placeholder"))
