"""Detail views, one per object kind, and the registry that finds them.

Why a registry and not a `ContentSwitcher`: a switcher needs every child
mounted up front. With a growing set of object kinds that means a dozen widget
trees resident at all times, each needing a coherent "nothing selected" state,
each subscribing to cache updates it does not care about. Mounting on demand
keeps exactly one view alive -- the one being looked at.

The pairing rule that makes this worth it: a view owns *both* its detail body
and whatever it contributes to the context bar above. They are one concept
("what you can do with a pipe"), and splitting them across two registries would
guarantee they drift apart.

Registration is a module-level dict populated by a decorator at import time.
No entry points, nothing to configure -- adding a kind is one new module plus
one import.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from textual.app import ComposeResult
from textual.widget import Widget

from snowcone.cache.repository import Repository
from snowcone.models import ObjectKind, ObjectRef, Scope
from snowcone.ui.messages import ScopeNeeded

_registry: dict[ObjectKind, type[ObjectView]] = {}
_category_registry: dict[ObjectKind, type[CategoryView]] = {}


class _ScopedView(Widget):
    """What `ObjectView` and `CategoryView` share: repainting from the cache
    and asking for what is missing or stale.

    Split out because a category view (keyed by a `Scope`, no single object)
    is not a rarer kind of `ObjectView` -- it has no `ref` and no `fqn` -- but
    it needs the exact same freshness and cache-update plumbing.
    """

    def cache_updated(self, scope: Scope) -> None:
        """Repaint if the scope that changed is one this view is showing."""
        if scope in self.scopes():
            self.update_content()

    def scopes(self) -> list[Scope]:
        """Cache scopes this view reads, for freshness and the countdown."""
        return []

    def compose_context(self) -> ComposeResult:
        """Widgets this view contributes to the context bar above."""
        yield from ()

    @abstractmethod
    def update_content(self) -> None:
        """Repaint from the cache. Called on mount and on every change."""

    # ------------------------------------------------------------ freshness

    def ensure(self, scope: Scope, ttl: float, *, interactive: bool = True) -> bool:
        """Ask for `scope` if it is missing or stale. Returns whether it asked."""
        age = self.repo.entry(scope).age()
        if age is None or (ttl > 0 and age >= ttl):
            self.post_message(ScopeNeeded(scope, interactive=interactive))
            return True
        return False


class ObjectView(_ScopedView):
    """One object kind's detail UI, mounted into the detail pane on demand."""

    OBJECT_KIND: ClassVar[ObjectKind]

    def __init__(self, repo: Repository, ref: ObjectRef) -> None:
        super().__init__()
        self.repo = repo
        self.ref = ref

    def on_mount(self) -> None:
        self.update_content()

    def show(self, ref: ObjectRef) -> None:
        """Re-point an already mounted view at another object of its kind.

        This is the path that makes moving between two tables cheap: the
        widget, its columns and its scroll position all survive, so there is
        no remount and no flicker.
        """
        self.ref = ref
        if self.is_mounted:
            self.update_content()


class CategoryView(_ScopedView):
    """One category's list UI (e.g. every table in a schema), mounted into
    the detail pane on demand -- the group-node counterpart to `ObjectView`.
    """

    OBJECT_KIND: ClassVar[ObjectKind]

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__()
        self.repo = repo
        self.scope = scope

    def on_mount(self) -> None:
        self.update_content()

    def show(self, scope: Scope) -> None:
        """Re-point an already mounted view at another category of the same
        kind -- e.g. moving to another schema's Tables node."""
        self.scope = scope
        if self.is_mounted:
            self.update_content()


def register_view[T: type[ObjectView]](cls: T) -> T:
    """Register a view for its OBJECT_KIND."""
    kind = cls.OBJECT_KIND
    if kind in _registry:
        raise ValueError(f"a view is already registered for {kind}")
    _registry[kind] = cls
    return cls


def view_for(kind: ObjectKind) -> type[ObjectView] | None:
    return _registry.get(kind)


def registered_kinds() -> frozenset[ObjectKind]:
    return frozenset(_registry)


def register_category_view[T: type[CategoryView]](cls: T) -> T:
    """Register a category (group-node) view for its OBJECT_KIND."""
    kind = cls.OBJECT_KIND
    if kind in _category_registry:
        raise ValueError(f"a category view is already registered for {kind}")
    _category_registry[kind] = cls
    return cls


def category_view_for(kind: ObjectKind) -> type[CategoryView] | None:
    return _category_registry.get(kind)


def registered_category_kinds() -> frozenset[ObjectKind]:
    return frozenset(_category_registry)
