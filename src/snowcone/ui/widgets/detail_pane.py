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
from snowcone.models import ObjectRef, Scope
from snowcone.ui.views.base import ObjectView, view_for


class DetailPane(Vertical):
    """Shows whatever object the tree or the search bar last selected."""

    def __init__(self, repo: Repository, **kwargs) -> None:
        super().__init__(**kwargs)
        self.repo = repo
        self._view: ObjectView | None = None
        self._ref: ObjectRef | None = None

    @property
    def view(self) -> ObjectView | None:
        return self._view

    @property
    def ref(self) -> ObjectRef | None:
        return self._ref

    def show(self, ref: ObjectRef) -> ObjectView | None:
        """Point the pane at an object. Returns the view now showing it.

        The return value is how the screen knows whether the context bar above
        needs rebuilding: a returned view identical to last time means the
        context is still correct.
        """
        self._ref = ref
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

    def clear(self) -> None:
        self._view = None
        self._ref = None
        self.remove_children()

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
