# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The two-pane browser.

This screen is the one place that knows about both the cache and the fetch
service, so it is where scope routing lives: a `CacheUpdated` names exactly one
scope, and it is handed to the one widget that shows it rather than broadcast.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib.resources import files
from math import ceil
from typing import ClassVar

from tcoda.dialogs import AboutDialog, SettingsDialog
from tcoda.widgets import ChordViewer, Countdown, Footer, LogViewer
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from nivaria.cache.repository import Repository
from nivaria.gateway.pool import ConnectionPool
from nivaria.models import (
    ObjectRef,
    SchemaRef,
    Scope,
    ScopeType,
    Warehouse,
    databases_scope,
    filling_prime,
    prime_scope,
    roles_scope,
    warehouses_scope,
)
from nivaria.profiles import ProfileStore
from nivaria.service.fetch import FetchService
from nivaria.service.jobs import Priority, log_policy
from nivaria.settings import CACHE_TTL, SNOWSIGHT_URL, config
from nivaria.snowsight import base_url, database_url, object_url, schema_url
from nivaria.ui.messages import (
    CacheUpdated,
    CategorySelected,
    DatabaseSelected,
    ObjectOpened,
    ObjectSelected,
    SchemaSelected,
    ScopeNeeded,
)
from nivaria.ui.screens.multipicker import MultiPickerDialog
from nivaria.ui.screens.picker import PickerDialog
from nivaria.ui.screens.search import SearchDialog
from nivaria.ui.views import (  # noqa: F401 -- registers views
    pipe_list_view,
    pipe_view,
    stage_list_view,
    table_list_view,
    table_view,
    task_list_view,
    task_view,
    view_list_view,
    view_view,
)
from nivaria.ui.widgets.detail_pane import DetailPane
from nivaria.ui.widgets.object_tree import NodeType, ObjectTree
from nivaria.ui.widgets.status_bar import PoolGauge, RoleLabel, WarehouseLabel
from nivaria.version import __version__

#: How often the status indicators are recomputed. Fast enough that a burst of
#: pool activity is visible rather than blinking past between ticks, and cheap
#: enough to run on the event loop: an int read plus one indexed sqlite lookup.
TICK = 0.3

#: The About dialog's text. A real filesystem Path even for an installed
#: (non-zipped) wheel, which is what AboutDialog needs -- it reads this with
#: Path.read_text() rather than importlib.resources' API.
ABOUT_PATH = files("nivaria.ui").joinpath("about.txt")


def _requestable(scope: Scope) -> Scope:
    """What to actually ask the fetch service for when `scope` is needed.

    A whole schema's objects are cheaper to fetch for the entire database at
    once -- one statement instead of a round trip per schema -- so an objects
    request is upgraded to a database-wide prime.

    The upgrade tests the scope's own type, not whether it happens to carry
    both an object type and a database. Several SELECT-backed scopes could
    plausibly share that shape; only OBJECTS actually means "a SHOW listing
    for one schema", which is the only thing a SHOW prime can stand in for.
    """
    if scope.type is not ScopeType.OBJECTS:
        return scope
    assert scope.object_type is not None
    assert scope.database is not None
    return prime_scope(scope.object_type, scope.database)


class MainScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        # Only the footer's entry for the chords below. `ChordMixin` takes the
        # key before any binding sees it, so a press or a click on the entry
        # starts a chord and this action never runs.
        Binding("comma", "app.bell", "Menu"),
        # On the screen rather than the app, so that they do not exist on the
        # connect screen. Hidden from the footer, not from `ChordViewer`, which
        # is where they are found.
        Binding("comma a c", "open_connect", "Connect", show=False),
        Binding("comma a w", "pick_warehouse", "Warehouse selection", show=False),
        Binding("comma a r", "pick_role", "Role selection", show=False),
        Binding("comma a d", "pick_databases", "Database selection", show=False),
        Binding("comma s", "settings", "Settings", show=False),
        Binding("comma ?", "about", "About", show=False),
        Binding("slash", "search", "Search"),
        Binding("f5", "refresh", "Refresh"),
        # "Web", not "Snowsight": a label starts with its key's letter, and
        # `s` is already the SQL tab's.
        Binding("w", "open_in_snowsight", "Web"),
    ]

    CHORD_GROUPS: ClassVar[dict[str, str]] = {"comma a": "Account"}

    def __init__(
        self,
        repo: Repository,
        fetch: FetchService | None = None,
        pool: ConnectionPool | None = None,
        profiles: ProfileStore | None = None,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.fetch = fetch
        self.pool = pool
        self.profiles = profiles or ProfileStore()
        self.databases: list[str] = []

    def compose(self) -> ComposeResult:
        yield LogViewer(show_key=False)
        tree = ObjectTree(self.repo, id="tree")
        tree.border_title = "Databases"
        yield tree
        yield DetailPane(self.repo, id="detail")
        # The chord key is pinned, so it holds the left corner
        # whatever the focus chain is doing. Nothing reserves the right
        # corner any more: this footer gives ctrl+p no special treatment,
        # so the status area simply sits there.
        with Footer(pinned=("comma",)):
            yield RoleLabel(id="footer-role")
            yield WarehouseLabel(id="footer-warehouse")
            yield Countdown(id="footer-countdown")
            yield PoolGauge(id="footer-pool")
        yield ChordViewer()

    def on_mount(self) -> None:
        self.prefetch_session_lists()
        self.set_interval(TICK, self._tick)
        self._tick()

    def prefetch_session_lists(self) -> None:
        """Ask for what the database, warehouse and role pickers offer.

        Warehouses and roles too, not just databases: both are cheap, unpaged
        SHOW calls, and fetching them lazily on the picker's first open meant
        the dialog didn't have anything to show yet -- one keypress to request
        the list, a second to actually get the picker.
        """
        self._request(databases_scope(), Priority.INTERACTIVE)
        self._request(warehouses_scope(), Priority.INTERACTIVE)
        self._request(roles_scope(), Priority.INTERACTIVE)

    @property
    def countdown(self) -> Countdown:
        return self.query_one(Countdown)

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
        # No single canonical database to prefer here any more -- multiple
        # can be selected. Only affects the brief pre-navigation window
        # before the cursor lands anywhere.
        return databases_scope()

    def _tick(self) -> None:
        countdown = self.countdown

        if self.pool is not None:
            gauge = self.query_one(PoolGauge)
            gauge.in_use = self.pool.in_use
            gauge.pool_size = self.pool.size
            self.query_one(WarehouseLabel).warehouse = self.pool.params.warehouse
            self.query_one(RoleLabel).role = self.pool.params.role

        # focused_scope() is kept in the mix so the spinner still reflects
        # activity before anything is materialised in the tree or detail pane
        # yet (e.g. the initial database-list fetch at startup); the other
        # two are what let it also catch a busy detail-pane-only scope (e.g.
        # a pipe's own load history) that the tree cursor's own scope knows
        # nothing about.
        watched = [
            self.focused_scope(),
            *self.tree.visible_scopes(),
            *self.detail.scopes(),
        ]
        # The countdown runs its own spinner off this, so there is no frame
        # counter to drive from here any more.
        countdown.refreshing = any(self._busy(scope) for scope in watched)

        # Read at the point of use, so changing it in settings takes effect
        # without a restart.
        ttl = config.get(CACHE_TTL)
        # Hidden, not zeroed: a countdown that blanked itself at zero could
        # not tell "no staleness sweep is scheduled" apart from "the next one
        # is due right now".
        countdown.display = ttl > 0
        if ttl <= 0:
            return

        # Derived from the ledger on every tick rather than run as a timer:
        # a timer restarted at the full TTL ran out just before a refresh it
        # had itself triggered went stale, so that data waited a second TTL.
        due = self.repo.next_due(ttl, self._idle_watched())
        if due is not None and due <= 0:
            self._sweep()
            due = self.repo.next_due(ttl, self._idle_watched())
        countdown.remaining = ttl if due is None else max(0, ceil(due))

    def _busy(self, scope: Scope) -> bool:
        """Queued or mid-fetch, itself or through the prime that fills it."""
        if self.fetch is None:
            return False
        prime = filling_prime(scope)
        return self.fetch.is_busy(scope) or (
            prime is not None and self.fetch.is_busy(prime)
        )

    def _idle_watched(self) -> list[Scope]:
        """The scopes on screen that nothing is fetching yet.

        What the sweep refreshes and the countdown promises about. A busy
        scope's old fetched_at reads as overdue until its pass lands, which
        the spinner covers; and requesting one a prime is filling would be
        the per-schema round trip the prime exists to replace.
        """
        return [
            scope
            for scope in [*self.tree.visible_scopes(), *self.detail.scopes()]
            if not self._busy(scope)
        ]

    def _sweep(self) -> None:
        """Refresh what is on screen and has gone stale, nothing else."""
        if self.fetch is None:
            return
        ttl = config.get(CACHE_TTL)
        if ttl <= 0:
            return
        # The expanded schemas were filled by one prime and go stale
        # together, so requesting each as itself would be a round trip per
        # schema and category. Several of them naming the same prime queue it
        # once: FetchService.request deduplicates.
        for scope in self.repo.stale_scopes(ttl, self._idle_watched()):
            self._request(_requestable(scope), Priority.BACKGROUND)

    @property
    def tree(self) -> ObjectTree:
        return self.query_one("#tree", ObjectTree)

    @property
    def detail(self) -> DetailPane:
        return self.query_one("#detail", DetailPane)

    @work
    async def action_search(self) -> None:
        hit = await self.app.push_screen_wait(SearchDialog(self.repo, self.databases))
        if hit is None:
            return
        if isinstance(hit, SchemaRef):
            # A schema has no detail view to open (see SchemaRef) -- reveal
            # and expand it in the tree, the same place _select leaves you
            # for an object, and stop there.
            self.tree.reveal_schema(hit)
            return
        self._select(hit)

    # A worker, like every action below that waits on a dialog:
    # `push_screen_wait` needs a worker context.
    @work
    async def action_open_connect(self) -> None:
        await self.app.open_connect()

    def action_about(self) -> None:
        self.app.push_screen(
            AboutDialog(ABOUT_PATH, variables={"version": __version__})
        )

    def action_settings(self) -> None:
        self.app.push_screen(SettingsDialog())

    # --------------------------------------------------------------- routing

    def on_cache_updated(self, event: CacheUpdated) -> None:
        self._route_cache_update(event.scope)

    def _route_cache_update(self, scope: Scope) -> None:
        self.tree.cache_updated(scope)
        self.detail.cache_updated(scope)

    def on_object_selected(self, event: ObjectSelected) -> None:
        # Show, not _select: the cursor is already on the object, and
        # revealing it again queues a cursor move of its own. Textual
        # re-highlights a node whose line merely shifted, so a highlight
        # that moves the cursor can answer another one forever -- see
        # docs/DESIGN.md "A highlight is not a jump".
        self.detail.show(event.ref)

    def on_object_opened(self, event: ObjectOpened) -> None:
        self._select(event.ref)

    def on_category_selected(self, event: CategorySelected) -> None:
        self._select_category(event.scope)

    def on_schema_selected(self, event: SchemaSelected) -> None:
        # No tree.reveal(...) needed here either, same reasoning as
        # _select_category: the cursor is already on the schema node that
        # was just highlighted.
        self.detail.show_schema(event.ref)

    def on_database_selected(self, event: DatabaseSelected) -> None:
        self.detail.show_database(event.database)

    def _select_category(self, scope: Scope) -> None:
        # No tree.reveal(...) needed here, unlike _select: the cursor is
        # already on the category node that was just highlighted.
        self.detail.show_category(scope)

    def _select(self, ref: ObjectRef) -> None:
        # A search hit or an Enter in a list view leaves the tree somewhere
        # you can keep browsing from, rather than a detail pane pointing at
        # an object the tree cursor is not on. A jump from outside the tree
        # only: see on_object_selected for why a tree highlight must not
        # come here.
        self.tree.reveal(ref)
        self.detail.show(ref)

    def on_scope_needed(self, event: ScopeNeeded) -> None:
        """The tree found something missing or stale."""
        priority = Priority.INTERACTIVE if event.interactive else Priority.VISIBLE
        self._request(_requestable(event.scope), priority)

    def _request(self, scope: Scope, priority: Priority) -> None:
        if self.fetch is not None:
            self.fetch.request(scope, priority)

    # ----------------------------------------------------------------- repo

    def set_repo(self, repo: Repository) -> None:
        """Point every cache-reading widget at a different Repository.

        Used for a role switch or a reconnect to a different identity -- see
        `NivariaApp.switch_repo`. Whatever was on screen was read out of the
        old identity's cache file and cannot simply be relabelled against the
        new one (a different file entirely), so this resets browsing state
        the same way a fresh connect would: no databases selected, nothing in
        the detail pane. Search reads the cache fresh each time it opens, so
        there is nothing to repoint there.
        """
        self.repo = repo
        self.databases = []
        self.tree.set_repo(repo)
        self.detail.set_repo(repo)

    # ------------------------------------------------------------- databases

    def set_databases(self, databases: list[str]) -> None:
        if databases == self.databases:
            return
        removed = set(self.databases) - set(databases)
        self.databases = databases
        if self.fetch is not None and removed:
            # Whatever was still queued for a removed database is work
            # nobody is looking at any more.
            self.fetch.cancel_where(lambda scope: scope.database in removed)
        self.tree.set_databases(databases)
        self.detail.clear()

    @work
    async def action_pick_databases(self) -> None:
        """Open the database picker."""
        cached = self.repo.databases(databases_scope())
        if cached.is_empty:
            # Nothing to choose from yet; ask, and let the user try again once
            # the answer lands rather than opening an empty dialog.
            self._request(databases_scope(), Priority.INTERACTIVE)
            self.notify("Loading databases...", timeout=3)
            return
        chosen = await self.app.push_screen_wait(
            MultiPickerDialog(
                [row.name for row in cached.rows],
                title="Databases",
                placeholder="type to filter databases",
                current=self.databases,
            )
        )
        if chosen is not None:
            self.set_databases(chosen)
            self._remember_databases(chosen)

    def _remember_databases(self, databases: list[str]) -> None:
        """Fold the just-picked selection into this identity's Profile.

        Deliberately separate from set_databases: that method is also called
        by the set_repo reset path (an empty list, on a role switch or
        reconnect), and there is nothing to remember about *that* -- only an
        actual picker confirmation should write anything.

        Not deferred to the next successful connect the way a brand-new
        Profile is (see ConnectScreen._finish): the pool here is already a
        working connection, so there is no "does this even work" gate left
        to wait on -- this is a live UI preference riding on the same
        record. ProfileStore.remember also refreshes role/warehouse to
        whatever is currently live, which is a deliberate, desirable side
        effect rather than an accident: the record should describe this
        session as it actually is now. The name is not this screen's to set,
        and `remember` keeps it.
        """
        if self.pool is None:
            return
        self.profiles.remember(self.pool.params, databases=tuple(databases))

    # ------------------------------------------------------ warehouse / role

    @work
    async def action_pick_warehouse(self) -> None:
        """Open the warehouse picker."""
        if self.pool is None:
            return
        cached = self.repo.warehouses(warehouses_scope())
        if cached.is_empty:
            self._request(warehouses_scope(), Priority.INTERACTIVE)
            self.notify("Loading warehouses...", timeout=3)
            return
        current = self.pool.params.warehouse
        chosen = await self.app.push_screen_wait(
            PickerDialog(
                [row.name for row in cached.rows],
                title="Warehouse",
                placeholder="type to filter warehouses",
                current=current,
                hints=self._warehouse_hints(cached.rows),
            )
        )
        if chosen is not None and chosen != current:
            self._switch_warehouse(chosen)

    @staticmethod
    def _warehouse_hints(rows: Sequence[Warehouse]) -> dict[str, list[str]]:
        """Size and, when set, `default` -- size first, since it is the more
        common reason to look twice at a row."""
        hints: dict[str, list[str]] = {}
        for row in rows:
            parts = [row.size] if row.size else []
            if row.is_default:
                parts.append("default")
            if parts:
                hints[row.name] = parts
        return hints

    @work
    async def action_pick_role(self) -> None:
        """Open the role picker."""
        if self.pool is None:
            return
        cached = self.repo.roles(roles_scope())
        if cached.is_empty:
            self._request(roles_scope(), Priority.INTERACTIVE)
            self.notify("Loading roles...", timeout=3)
            return
        current = self.pool.params.role
        chosen = await self.app.push_screen_wait(
            PickerDialog(
                [row.name for row in cached.rows],
                title="Role",
                placeholder="type to filter roles",
                current=current,
            )
        )
        if chosen is not None and chosen != current:
            self._switch_role(chosen)

    @work(thread=True)
    def _switch_warehouse(self, warehouse: str) -> None:
        """Apply the switch pool-wide. Threaded: `ConnectionPool.set_warehouse`
        blocks until every connection is idle -- see `ConnectionPool.run_on_all`.
        """
        assert self.pool is not None
        try:
            self.pool.set_warehouse(warehouse)
        # A worker thread boundary, same as fetch.py: the driver raises many
        # unrelated types, and any escape here would kill the thread silently.
        except Exception as error:  # noqa: BLE001
            message = str(error) or error.__class__.__name__
            self.app.call_from_thread(
                self.notify,
                f"Could not switch warehouse: {message}",
                severity="error",
                timeout=8,
            )
            return
        self.app.call_from_thread(self._retry_failed)

    def _retry_failed(self) -> None:
        """Refetch whatever on screen failed, now that a warehouse is set.

        Without a warehouse every SELECT-backed scope on screen fails, and
        the sweep only retries a failed scope one TTL after it failed: left
        to that, the warehouse that fixes them would show nothing for
        minutes.
        """
        for scope in self.repo.failed_scopes(self._idle_watched()):
            self._request(scope, Priority.VISIBLE)

    @work(thread=True)
    def _switch_role(self, role: str) -> None:
        """Apply the switch, then repoint the app at that role's own cache.

        The fetch service is stopped *before* the role itself changes, not
        after: `pool.set_role` returning means every connection already
        carries the new role, so a worker still bound to the old repo that
        drew one of them afterward would write new-role results into the old
        role's cache file -- the exact bleed a file-per-identity cache exists
        to rule out. Stopping first keeps every later job out; a worker that
        `stop()` gave up on, still mid-query, is the stopped service's to
        silence (see `FetchService._stopped`): `pool.set_role` waits for its
        statement, but a retry of it would run under the new role.
        """
        assert self.pool is not None
        if self.fetch is not None:
            self.fetch.stop()
        try:
            self.pool.set_role(role)
        # A worker thread boundary, same as fetch.py: the driver raises many
        # unrelated types, and any escape here would kill the thread silently.
        except Exception as error:  # noqa: BLE001
            message = str(error) or error.__class__.__name__
            self.app.call_from_thread(
                self.notify,
                f"Could not switch role: {message}",
                severity="error",
                timeout=8,
            )
            # The role never changed, so the same cache is still correct --
            # only the fetch service this method stopped needs restarting.
            self.app.call_from_thread(self._role_switch_failed)
            return
        self.app.call_from_thread(self._role_switched)

    def _role_switched(self) -> None:
        assert self.pool is not None
        self.app.switch_repo(self.pool)
        self.fetch = self.app.fetch
        self.set_repo(self.app.repo)
        # Prefetched immediately rather than waiting for the picker's next
        # open, same reasoning as on_mount: a brand-new role's cache file has
        # nothing in it yet, and even a previously-visited one may have gone
        # stale while this role was not the active one.
        self.prefetch_session_lists()

    def _role_switch_failed(self) -> None:
        assert self.pool is not None
        self.app.restart_fetch(self.pool)
        self.fetch = self.app.fetch

    def action_refresh(self) -> None:
        """Explicitly refresh whatever the cursor is on, and what the detail
        pane shows for it.

        The detail pane's scopes are asked for whatever their age. A log
        scope's refresh is a window pass like any other (see
        `repository.plan_window`), so this costs no more there than the
        sweep's own refresh would.
        """
        for scope in self.detail.scopes():
            self._request(_requestable(scope), Priority.INTERACTIVE)
        node = self.tree.cursor_node
        if node is not None and node.data and node.data.type is NodeType.SCHEMA:
            # A schema node owns no scope; the list it appears in is its
            # database's, and refreshing that is what drops it if it is gone.
            node = node.parent
        scope = node.data.scope if node is not None and node.data else None
        if scope is None:
            scope = databases_scope() if not self.databases else None
        if scope is not None:
            self._request(scope, Priority.INTERACTIVE)

    def full_refresh(self) -> None:
        """Refetch the detail pane's log scopes in full, ignoring what they
        already cover.

        The escape hatch for a window pass that missed a row: one Snowflake
        published later than the lag margin, which no later window asks for
        again. Each scope's coverage is forgotten first, so its next pass is
        a full one, then they are asked for now. Deliberately bound to no key
        yet; the UI that offers it is still to come.
        """
        for scope in self.detail.scopes():
            if log_policy(scope) is None:
                continue
            self.repo.forget_coverage(scope)
            self._request(scope, Priority.INTERACTIVE)

    def action_open_in_snowsight(self) -> None:
        """Open whatever the cursor is on in Snowsight."""
        node = self.tree.cursor_node
        if self.pool is None or node is None or node.data is None:
            return
        base = base_url(
            self.pool.account_names,
            privatelink=self.pool.params.uses_privatelink,
            override=config.get(SNOWSIGHT_URL),
        )
        if base is None:
            # One message for both causes (a PrivateLink account, or names
            # that could not be read at connect): the setting fixes either.
            self.notify(
                "No Snowsight link for this account: set its Snowsight URL "
                "in Settings.",
                severity="warning",
                timeout=5,
            )
            return
        data = node.data
        match data.type:
            case NodeType.DATABASE:
                assert data.database is not None
                url = database_url(base, data.database)
            case NodeType.SCHEMA:
                assert data.database is not None
                url = schema_url(base, data.database, data.key)
            case NodeType.CATEGORY:
                # Snowsight has no page for "the pipes of a schema", so a
                # category opens the schema it belongs to.
                assert data.scope is not None
                assert data.database is not None
                assert data.scope.schema is not None
                url = schema_url(base, data.database, data.scope.schema)
            case NodeType.OBJECT:
                assert data.ref is not None
                url = object_url(base, data.ref)
            case _:
                return
        self.app.open_url(url)
