"""The container that mounts one detail view at a time.

The swap rule is the whole point: if the object being shown is of the same kind
as the one already mounted, the view is re-pointed rather than remounted. That
is what makes arrowing down a list of three hundred tables cheap -- the widget,
its columns and its scroll position all survive, and there is no flicker.
A different kind is a genuine change of shape, so that view is replaced.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Static

from snowcone.cache.repository import Repository
from snowcone.models import ObjectRef, SchemaRef, Scope
from snowcone.ui.views.base import (
    CategoryView,
    ObjectView,
    category_view_for,
    view_for,
)
from snowcone.ui.views.schema_view import DatabaseView, SchemaView


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
        """Point the pane at an object. Returns the view now showing it."""
        self._ref = ref
        self._scope = None
        self._schema = None
        self._database = None
        view_class = view_for(ref.kind)
        if view_class is None:
            self._show_placeholder(f"No detail view for {ref.kind.value} yet.")
            return None

        current = self._view
        if current is not None and type(current) is view_class:
            current.show(ref)
            return current

        self.remove_children()
        view = view_class(self.repo, ref)
        self._view = view
        self.mount(view)
        return view

    def show_category(self, scope: Scope) -> CategoryView | None:
        """Point the pane at a category node ("Tables (n)", ...).

        Same swap rule as `show`: same kind re-points the mounted view, a
        different kind (or one with no registered view) replaces it.
        """
        self._ref = None
        self._scope = scope
        self._schema = None
        self._database = None
        kind = scope.object_kind
        view_class = category_view_for(kind) if kind is not None else None
        if view_class is None:
            label = kind.plural if kind is not None else "this"
            self._show_placeholder(f"No list view for {label} yet.")
            return None

        current = self._view
        if current is not None and type(current) is view_class:
            current.show(scope)
            return current

        self.remove_children()
        view = view_class(self.repo, scope)
        self._view = view
        self.mount(view)
        return view

    def show_schema(self, ref: SchemaRef) -> SchemaView:
        """Point the pane at a schema's Grants/Tags view.

        No registry lookup, unlike `show`/`show_category`: there is exactly
        one view for a schema node, not one of several kinds sharing a
        lookup, so this constructs SchemaView directly.
        """
        self._ref = None
        self._scope = None
        self._database = None
        self._schema = ref

        current = self._view
        if type(current) is SchemaView:
            current.show(ref)
            return current

        self.remove_children()
        view = SchemaView(self.repo, ref)
        self._view = view
        self.mount(view)
        return view

    def show_database(self, database: str) -> DatabaseView:
        """Point the pane at a database's Grants/Tags view. See `show_schema`."""
        self._ref = None
        self._scope = None
        self._schema = None
        self._database = database

        current = self._view
        if type(current) is DatabaseView:
            current.show(database)
            return current

        self.remove_children()
        view = DatabaseView(self.repo, database)
        self._view = view
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
        """What the pane is reading, for the staleness sweep."""
        return self._view.scopes() if self._view is not None else []

    def _show_placeholder(self, message: str) -> None:
        self._view = None
        self.remove_children()
        self.mount(Static(message, id="detail-placeholder"))
