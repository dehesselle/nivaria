"""Messages passed between snowcone's widgets.

Kept in one module rather than nested inside whichever widget happens to post
them, because the tree and the detail views raise the same intents: both need
to say "this scope is stale" without either knowing the other exists, and the
screen routes both through one handler.
"""

from __future__ import annotations

from textual.message import Message

from snowcone.models import ObjectRef, Scope


class CacheUpdated(Message):
    """One cache scope changed. Routed to what shows it, never broadcast."""

    def __init__(self, scope: Scope, complete: bool) -> None:
        self.scope = scope
        self.complete = complete
        super().__init__()


class ScopeNeeded(Message):
    """Something on screen is showing a scope that is missing or stale.

    Posted rather than calling the fetch service, so widgets stay ignorant of
    scheduling and a test can assert on intent instead of on side effects.
    """

    def __init__(self, scope: Scope, *, interactive: bool) -> None:
        self.scope = scope
        self.interactive = interactive
        super().__init__()


class ObjectSelected(Message):
    """A browsable object is under the cursor."""

    def __init__(self, ref: ObjectRef) -> None:
        self.ref = ref
        super().__init__()


class CategorySelected(Message):
    """A category node ("Tables (n)", "Views (n)", ...) is under the cursor."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        super().__init__()


class MenuTitleClicked(Message):
    """A menu bar title was clicked. Bubbles to the screen, which owns menu
    contents and dropdown behavior."""

    def __init__(self, menu_id: str) -> None:
        self.menu_id = menu_id
        super().__init__()
