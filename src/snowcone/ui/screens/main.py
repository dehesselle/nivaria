"""The three-pane browser.

This screen is the one place that knows about both the cache and the fetch
service, so it is where scope routing lives: a `CacheUpdated` names exactly one
scope, and it is handed to the one widget that shows it rather than broadcast.
"""

from __future__ import annotations

from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from snowcone.cache.repository import Repository
from snowcone.models import Scope, databases_scope, prime_scope
from snowcone.service.fetch import FetchService
from snowcone.service.jobs import Priority
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.screens.picker import PickerScreen
from snowcone.ui.widgets.object_tree import ObjectTree

NO_DATABASE = "(none)"


class CacheUpdated(Message):
    """One cache scope changed. Routed, never broadcast."""

    def __init__(self, scope: Scope, complete: bool) -> None:
        self.scope = scope
        self.complete = complete
        super().__init__()


class MainScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+d", "pick_database", "Database"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(
        self,
        repo: Repository,
        fetch: FetchService | None = None,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.fetch = fetch
        self.database: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="left"):
            yield Static(f"DB  {NO_DATABASE}", id="db-button")
            yield ObjectTree(self.repo, ttl=config.get(CACHE_TTL), id="tree")
        with Vertical(id="right"):
            yield Vertical(id="context")
            yield Vertical(id="detail")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self._request(databases_scope(), Priority.INTERACTIVE)

    @property
    def tree(self) -> ObjectTree:
        return self.query_one("#tree", ObjectTree)

    # --------------------------------------------------------------- routing

    def on_cache_updated(self, event: CacheUpdated) -> None:
        self.tree.cache_updated(event.scope)

    def on_object_tree_scope_needed(self, event: ObjectTree.ScopeNeeded) -> None:
        """The tree found something missing or stale.

        A whole schema's objects are cheaper to fetch for the entire database
        at once -- one statement instead of a round trip per schema -- so an
        objects request is upgraded to a database-wide prime.
        """
        scope = event.scope
        priority = Priority.INTERACTIVE if event.interactive else Priority.VISIBLE
        if scope.object_kind is not None and scope.database is not None:
            self._request(prime_scope(scope.object_kind, scope.database), priority)
            return
        self._request(scope, priority)

    def _request(self, scope: Scope, priority: Priority) -> None:
        if self.fetch is not None:
            self.fetch.request(scope, priority)

    # -------------------------------------------------------------- database

    def set_database(self, database: str | None) -> None:
        if database == self.database:
            return
        previous = self.database
        self.database = database
        if self.fetch is not None and previous is not None:
            # Whatever was still queued for the old database is work nobody is
            # looking at any more.
            self.fetch.cancel_where(lambda scope: scope.database == previous)
        self.query_one("#db-button", Static).update(f"DB  {database or NO_DATABASE}")
        self.tree.set_database(database)

    def action_pick_database(self) -> None:
        self._pick_database()

    @work
    async def _pick_database(self) -> None:
        cached = self.repo.databases(databases_scope())
        if cached.is_empty:
            # Nothing to choose from yet; ask, and let the user try again once
            # the answer lands rather than opening an empty dialog.
            self._request(databases_scope(), Priority.INTERACTIVE)
            self.notify("Loading databases...", timeout=3)
            return
        chosen = await self.app.push_screen_wait(
            PickerScreen(
                [row.name for row in cached.rows],
                title="Database",
                placeholder="type to filter databases",
                current=self.database,
            )
        )
        if chosen is not None:
            self.set_database(chosen)

    def action_refresh(self) -> None:
        """Explicitly refresh whatever the cursor is on."""
        node = self.tree.cursor_node
        scope = node.data.scope if node is not None and node.data else None
        if scope is None:
            scope = databases_scope() if self.database is None else None
        if scope is not None:
            self._request(scope, Priority.INTERACTIVE)
