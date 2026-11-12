# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Messages passed between nivaria's widgets.

Kept in one module rather than nested inside whichever widget happens to post
them, because the tree and the detail views raise the same intents: both need
to say "this scope is stale" without either knowing the other exists, and the
screen routes both through one handler.
"""

from __future__ import annotations

from textual.message import Message

from nivaria.journal import Reason
from nivaria.models import ObjectRef, SchemaRef, Scope
from nivaria.service.jobs import Priority


class CacheUpdated(Message):
    """One cache scope changed. Routed to what shows it, never broadcast.

    `priority` is what the fetch answered (`FetchService._answered`): a view
    repainting on it asks for whatever else it now needs at that priority.
    """

    def __init__(
        self, scope: Scope, complete: bool, priority: Priority = Priority.INTERACTIVE
    ) -> None:
        self.scope = scope
        self.complete = complete
        self.priority = priority
        super().__init__()


class ScopeNeeded(Message):
    """Something on screen is showing a scope that is missing or stale.

    Posted rather than calling the fetch service, so widgets stay ignorant of
    scheduling and a test can assert on intent instead of on side effects.

    `priority` is what it is asked at: interactive for what the user just did,
    and for a repaint the priority of the fetch it answers, so a sweep's
    result never asks for more than the sweep would.

    `from_update` marks one asked from a repaint answering a cache update, which
    never asks for a scope that failed. It may have failed by the time this
    message is handled, so `MainScreen.on_scope_needed` looks again.

    `reason` and `detail` go to the journal: the trigger, and why the scope
    was due.
    """

    def __init__(
        self,
        scope: Scope,
        *,
        priority: Priority,
        from_update: bool = False,
        reason: Reason = Reason.UNKNOWN,
        detail: str = "",
    ) -> None:
        self.scope = scope
        self.priority = priority
        self.from_update = from_update
        self.reason = reason
        self.detail = detail
        super().__init__()


class ObjectSelected(Message):
    """A browsable object is under the cursor."""

    def __init__(self, ref: ObjectRef) -> None:
        self.ref = ref
        super().__init__()


class ObjectOpened(Message):
    """An object was chosen from outside the tree, so it is revealed there
    as well as shown. Not `ObjectSelected`: that one is the tree's own
    highlight, which must never reveal (see docs/DESIGN.md "A highlight is not a
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
