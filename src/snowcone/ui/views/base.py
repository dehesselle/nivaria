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
from typing import ClassVar

from textual.widget import Widget

from snowcone.cache.repository import Repository
from snowcone.models import ObjectRef, ObjectType, Scope, is_stale
from snowcone.ui.messages import ScopeNeeded

_registry: dict[ObjectType, type[ObjectView]] = {}
_category_registry: dict[ObjectType, type[CategoryView]] = {}


class _ScopedView(Widget):
    """What `ObjectView` and `CategoryView` share: repainting from the cache
    and asking for what is missing or stale.

    Split out because a category view (keyed by a `Scope`, no single object)
    is not a rarer kind of `ObjectView` -- it has no `ref` and no `fqn` -- but
    it needs the exact same freshness and cache-update plumbing.
    """

    #: Set by every concrete subclass. Declared here because `ensure` below
    #: reads it, so the contract is visible where it is relied on rather than
    #: only in the three constructors that happen to satisfy it.
    repo: Repository

    def cache_updated(self, scope: Scope) -> None:
        """Repaint if the scope that changed is one this view is showing."""
        if scope in self.scopes():
            self.update_content()

    def scopes(self) -> list[Scope]:
        """Cache scopes this view reads, for freshness and the countdown."""
        return []

    @abstractmethod
    def update_content(self) -> None:
        """Repaint from the cache. Called on mount and on every change."""

    # ------------------------------------------------------------ freshness

    def ensure(self, scope: Scope, ttl: float, *, interactive: bool = True) -> bool:
        """Ask for `scope` if it is missing or stale. Returns whether it asked."""
        if is_stale(scope, self.repo.entry(scope).age(), ttl):
            self.post_message(ScopeNeeded(scope, interactive=interactive))
            return True
        return False


class ObjectView(_ScopedView):
    """One object type's detail UI, mounted into the detail pane on demand."""

    OBJECT_TYPE: ClassVar[ObjectType]

    def __init__(self, repo: Repository, ref: ObjectRef) -> None:
        super().__init__()
        self.repo = repo
        self.ref = ref

    def on_mount(self) -> None:
        self.update_content()

    def show(self, ref: ObjectRef) -> None:
        """Re-point an already mounted view at another object of its type.

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

    OBJECT_TYPE: ClassVar[ObjectType]

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__()
        self.repo = repo
        self.scope = scope

    def on_mount(self) -> None:
        self.update_content()

    def show(self, scope: Scope) -> None:
        """Re-point an already mounted view at another category of the same
        type -- e.g. moving to another schema's Tables node."""
        self.scope = scope
        if self.is_mounted:
            self.update_content()


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
