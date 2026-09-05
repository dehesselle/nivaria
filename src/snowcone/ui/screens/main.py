"""The three-pane browser.

This screen is the one place that knows about both the cache and the fetch
service, so it is where scope routing lives: a `CacheUpdated` names exactly one
scope, and it is handed to the one widget that shows it rather than broadcast.
"""

from __future__ import annotations

from math import ceil
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Header, Static

from snowcone.cache.repository import Repository
from snowcone.gateway.pool import ConnectionPool
from snowcone.models import Scope, databases_scope, prime_scope, schemas_scope
from snowcone.service.fetch import FetchService
from snowcone.service.jobs import Priority
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import CacheUpdated, ObjectSelected, ScopeNeeded
from snowcone.ui.screens.picker import PickerScreen
from snowcone.ui.views import pipe_view, table_view  # noqa: F401 -- registers views
from snowcone.ui.widgets.detail_pane import DetailPane
from snowcone.ui.widgets.object_tree import ObjectTree
from snowcone.ui.widgets.search_bar import SearchBar
from snowcone.ui.widgets.status_bar import StatusFooter

NO_DATABASE = "(none)"

#: How often the status indicators are recomputed. Fast enough that a burst of
#: pool activity is visible rather than blinking past between ticks, and cheap
#: enough to run on the event loop: an int read plus one indexed sqlite lookup.
TICK = 0.2

#: The background staleness sweep. Far slower than the tick because it walks
#: every scope currently on screen, and nothing it finds is urgent by
#: definition -- anything urgent was requested interactively.
SWEEP = 5.0


class MainScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+d", "pick_database", "Database"),
        Binding("slash", "search", "Search"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(
        self,
        repo: Repository,
        fetch: FetchService | None = None,
        pool: ConnectionPool | None = None,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.fetch = fetch
        self.pool = pool
        self.database: str | None = None
        self._frame = 0
        self._context_view = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="left"):
            yield Static(f"DB  {NO_DATABASE}", id="db-button")
            yield ObjectTree(self.repo, ttl=config.get(CACHE_TTL), id="tree")
        with Vertical(id="right"):
            with Vertical(id="context"):
                yield SearchBar(self.repo, id="search")
                yield Vertical(id="context-bar")
            yield DetailPane(self.repo, id="detail")
        yield StatusFooter(show_command_palette=False)

    def on_mount(self) -> None:
        self._request(databases_scope(), Priority.INTERACTIVE)
        self.set_interval(TICK, self._tick)
        self.set_interval(SWEEP, self._sweep)
        self._tick()

    @property
    def footer(self) -> StatusFooter:
        return self.query_one(StatusFooter)

    # -------------------------------------------------------------- status

    def focused_scope(self) -> Scope:
        """The scope backing whatever the cursor is on.

        Walks up from the cursor, because an object leaf owns no scope of its
        own -- the interesting age is that of the list it came from.
        """
        node = self.tree.cursor_node
        while node is not None:
            if node.data is not None and node.data.scope is not None:
                return node.data.scope
            node = node.parent
        if self.database is not None:
            return schemas_scope(self.database)
        return databases_scope()

    def _tick(self) -> None:
        footer = self.footer
        self._frame += 1
        footer.frame = self._frame

        if self.pool is not None:
            footer.in_use = self.pool.in_use
            footer.pool_size = self.pool.size

        # Read at the point of use, so changing it in settings takes effect
        # without a restart.
        ttl = config.get(CACHE_TTL)
        footer.countdown_enabled = ttl > 0

        scope = self.focused_scope()
        footer.refreshing = self.fetch is not None and self.fetch.is_busy(scope)
        if ttl <= 0:
            footer.remaining = 0
            return
        age = self.repo.entry(scope).age()
        # Never fetched reads as 0 rather than blank: it is maximally stale,
        # and something is already on its way to fix that.
        footer.remaining = 0 if age is None else max(0, ceil(ttl - age))

    def _sweep(self) -> None:
        """Refresh what is on screen and has gone stale, nothing else."""
        if self.fetch is None:
            return
        ttl = config.get(CACHE_TTL)
        if ttl <= 0:
            return
        watched = [*self.tree.visible_scopes(), *self.detail.scopes()]
        for scope in self.repo.stale_scopes(ttl, watched):
            self._request(scope, Priority.BACKGROUND)

    @property
    def tree(self) -> ObjectTree:
        return self.query_one("#tree", ObjectTree)

    @property
    def detail(self) -> DetailPane:
        return self.query_one("#detail", DetailPane)

    @property
    def search(self) -> SearchBar:
        return self.query_one("#search", SearchBar)

    def action_search(self) -> None:
        self.search.focus_query()

    # --------------------------------------------------------------- routing

    def on_cache_updated(self, event: CacheUpdated) -> None:
        self.tree.cache_updated(event.scope)
        self.detail.cache_updated(event.scope)
        self.search.cache_updated()

    def on_search_bar_activity(self, event: SearchBar.Activity) -> None:
        """The top pane shows one thing at a time: what you are looking at,
        or what you are looking for."""
        self.query_one("#context-bar", Vertical).display = not event.active

    def on_object_selected(self, event: ObjectSelected) -> None:
        # A search hit leaves the tree somewhere you can keep browsing from,
        # rather than a detail pane pointing at an object the tree has never
        # heard of.
        self.tree.reveal(event.ref)
        view = self.detail.show(event.ref)
        # Rebuilt only when the view instance actually changed: moving between
        # two tables keeps the same view, so its context is already correct.
        if view is not self._context_view:
            self._context_view = view
            self._rebuild_context(view)

    @work(exclusive=True, group="context")
    async def _rebuild_context(self, view) -> None:
        """Replace the context bar's contents.

        Awaited rather than fired and forgotten: remove_children() only
        *queues* the removal, so mounting the next view's widgets in the same
        tick collides with the outgoing ones on their ids. Exclusive, so a
        quick run through several object kinds cannot interleave two rebuilds.
        """
        bar = self.query_one("#context-bar", Vertical)
        await bar.remove_children()
        if view is not None:
            await bar.mount_all(list(view.compose_context()))

    def on_scope_needed(self, event: ScopeNeeded) -> None:
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
        self.search.set_database(database)
        self.detail.clear()
        self._context_view = None
        self._rebuild_context(None)

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
