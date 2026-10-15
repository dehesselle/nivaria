"""Messages passed between snowcone's widgets.

Kept in one module rather than nested inside whichever widget happens to post
them, because the tree and the detail views raise the same intents: both need
to say "this scope is stale" without either knowing the other exists, and the
screen routes both through one handler.
"""

from __future__ import annotations

from textual.message import Message

from snowcone.models import ObjectRef, SchemaRef, Scope


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


class ObjectOpened(Message):
    """An object was chosen from outside the tree, so it is revealed there
    as well as shown. Not `ObjectSelected`: that one is the tree's own
    highlight, which must never reveal (see DESIGN.md "A highlight is not a
    jump")."""

    def __init__(self, ref: ObjectRef) -> None:
        self.ref = ref
        super().__init__()


class CategorySelected(Message):
    """A category node ("Tables (n)", "Views (n)", ...) is under the cursor."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        super().__init__()


class SchemaSelected(Message):
    """A schema node is under the cursor."""

    def __init__(self, ref: SchemaRef) -> None:
        self.ref = ref
        super().__init__()


class DatabaseSelected(Message):
    """A database node is under the cursor."""

    def __init__(self, database: str) -> None:
        self.database = database
        super().__init__()
