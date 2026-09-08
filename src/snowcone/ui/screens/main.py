"""The two-pane browser.

This screen is the one place that knows about both the cache and the fetch
service, so it is where scope routing lives: a `CacheUpdated` names exactly one
scope, and it is handed to the one widget that shows it rather than broadcast.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from math import ceil
from typing import ClassVar

from tcw.widgets import Menu, MenuBar, MenuItem, MenuSeparator, SettingsMenuItem
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from snowcone.cache.repository import Repository
from snowcone.gateway.pool import ConnectionPool
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    SchemaRef,
    Scope,
    Warehouse,
    columns_prime_scope,
    databases_scope,
    prime_scope,
    roles_scope,
    warehouses_scope,
)
from snowcone.profiles import Profile, ProfileStore
from snowcone.service.fetch import FetchService
from snowcone.service.jobs import Priority
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import (
    CacheUpdated,
    CategorySelected,
    DatabaseSelected,
    ObjectSelected,
    SchemaSelected,
    ScopeNeeded,
)
from snowcone.ui.screens.multipicker import MultiPickerScreen
from snowcone.ui.screens.picker import PickerScreen
from snowcone.ui.screens.search import SearchScreen
from snowcone.ui.views import (  # noqa: F401 -- registers views
    pipe_list_view,
    pipe_view,
    table_list_view,
    table_view,
    task_list_view,
    task_view,
    view_list_view,
    view_view,
)
from snowcone.ui.widgets.detail_pane import DetailPane
from snowcone.ui.widgets.object_tree import ObjectTree
from snowcone.ui.widgets.status_bar import StatusFooter
from snowcone.version import VERSION

#: How often the status indicators are recomputed. Fast enough that a burst of
#: pool activity is visible rather than blinking past between ticks, and cheap
#: enough to run on the event loop: an int read plus one indexed sqlite lookup.
TICK = 0.2

#: The background staleness sweep. Far slower than the tick because it walks
#: every scope currently on screen, and nothing it finds is urgent by
#: definition -- anything urgent was requested interactively.
SWEEP = 5.0


class MainScreen(Screen):
    # No F9 here: `MenuBar` installs its own binding on whichever screen it
    # is mounted on, so the key and its footer entry come with the widget.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+d", "pick_databases", "Databases", show=False),
        Binding("ctrl+r", "pick_role", "Role", show=False),
        Binding("ctrl+w", "pick_warehouse", "Warehouse", show=False),
        Binding("slash", "search", "Search"),
        Binding("f5", "refresh", "Refresh"),
        Binding("f10", "app.quit", "Quit", show=False),
    ]

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
        self._frame = 0

    def compose(self) -> ComposeResult:
        # The nesting is the DOM structure, not style: a Menu is a child of
        # the bar, and collapsing the two into one `with` would stop reading
        # that way the moment a second menu is added.
        with MenuBar(trailing=f"snowcone {VERSION}"):  # noqa: SIM117
            # "connect-menu", not "connect": a Menu id is a CSS id too,
            # and app.tcss already styles #connect (the connect dialog).
            with Menu("Connect", id="connect-menu"):
                yield MenuItem("Open...", id="open", mnemonic="o")
                yield MenuSeparator()
                yield MenuItem(
                    "Databases ...", id="database", mnemonic="d", shortcut="Ctrl+D"
                )
                yield MenuItem(
                    "Warehouse ...", id="warehouse", mnemonic="w", shortcut="Ctrl+W"
                )
                yield MenuItem("Role ...", id="role", mnemonic="r", shortcut="Ctrl+R")
                yield MenuItem("Pre-warm cache", id="prewarm", mnemonic="p")
                yield MenuSeparator()
                yield SettingsMenuItem()
                yield MenuSeparator()
                yield MenuItem(
                    "Quit", id="quit", mnemonic="q", shortcut="F10", action="app.quit"
                )
        tree = ObjectTree(self.repo, ttl=config.get(CACHE_TTL), id="tree")
        yield tree
        yield DetailPane(self.repo, id="detail")
        yield StatusFooter(show_command_palette=False)

    def on_mount(self) -> None:
        self._request(databases_scope(), Priority.INTERACTIVE)
        # Prefetched too, not just databases: both are cheap, unpaged SHOW
        # calls, and fetching them lazily on the picker's first open meant the
        # dialog didn't have anything to show yet -- one keypress to request
        # the list, a second to actually get the picker.
        self._request(warehouses_scope(), Priority.INTERACTIVE)
        self._request(roles_scope(), Priority.INTERACTIVE)
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
        # No single canonical database to prefer here any more -- multiple
        # can be selected. Only affects the brief pre-navigation window
        # before the cursor lands anywhere.
        return databases_scope()

    def _tick(self) -> None:
        footer = self.footer
        self._frame += 1
        footer.frame = self._frame

        if self.pool is not None:
            footer.in_use = self.pool.in_use
            footer.pool_size = self.pool.size
            footer.warehouse = self.pool.params.warehouse
            footer.role = self.pool.params.role

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

    @work
    async def action_search(self) -> None:
        hit = await self.app.push_screen_wait(SearchScreen(self.repo, self.databases))
        if hit is None:
            return
        if isinstance(hit, SchemaRef):
            # A schema has no detail view to open (see SchemaRef) -- reveal
            # and expand it in the tree, the same place _select leaves you
            # for an object, and stop there.
            self.tree.reveal_schema(hit)
            return
        self._select(hit)

    # ------------------------------------------------------------- menu bar

    # The bar drives itself (F9, the staged bar/dropdown flow, mnemonics);
    # all that is left here is what each item means. Every handler hands off
    # to the same `@work` wrapper its key binding uses, because a plain
    # message handler has no worker context and `push_screen_wait` needs one.

    @on(MenuItem.Pressed, "#open")
    def _menu_open_connect(self) -> None:
        self._open_connect()

    @work
    async def _open_connect(self) -> None:
        await self.app.open_connect()

    @on(MenuItem.Pressed, "#database")
    def _menu_database(self) -> None:
        self._pick_databases()

    @on(MenuItem.Pressed, "#warehouse")
    def _menu_warehouse(self) -> None:
        self._pick_warehouse()

    @on(MenuItem.Pressed, "#role")
    def _menu_role(self) -> None:
        self._pick_role()

    @on(MenuItem.Pressed, "#prewarm")
    def _menu_prewarm(self) -> None:
        self.prewarm_cache()

    # --------------------------------------------------------------- routing

    def on_cache_updated(self, event: CacheUpdated) -> None:
        self._route_cache_update(event.scope)

    def _route_cache_update(self, scope: Scope) -> None:
        self.tree.cache_updated(scope)
        self.detail.cache_updated(scope)

    def on_object_selected(self, event: ObjectSelected) -> None:
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
        # A search hit leaves the tree somewhere you can keep browsing from,
        # rather than a detail pane pointing at an object the tree has never
        # heard of.
        self.tree.reveal(ref)
        self.detail.show(ref)

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

    # ----------------------------------------------------------------- repo

    def set_repo(self, repo: Repository) -> None:
        """Point every cache-reading widget at a different Repository.

        Used for a role switch or a reconnect to a different identity -- see
        `SnowconeApp.switch_repo`. Whatever was on screen was read out of the
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

    def action_pick_databases(self) -> None:
        self._pick_databases()

    @work
    async def _pick_databases(self) -> None:
        await self.pick_databases()

    async def pick_databases(self) -> None:
        """Open the database picker.

        Not decorated itself: both callers -- the `ctrl+d` binding and the
        menu item -- already wrap it in `_pick_databases`, and
        `push_screen_wait` needs exactly one worker context, not two.
        """
        cached = self.repo.databases(databases_scope())
        if cached.is_empty:
            # Nothing to choose from yet; ask, and let the user try again once
            # the answer lands rather than opening an empty dialog.
            self._request(databases_scope(), Priority.INTERACTIVE)
            self.notify("Loading databases...", timeout=3)
            return
        chosen = await self.app.push_screen_wait(
            MultiPickerScreen(
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
        record. ProfileStore.add's identity-based promotion also refreshes
        role/warehouse to whatever is currently live, which is a deliberate,
        desirable side effect of reusing that method rather than an
        accident: the record should describe this session as it actually is
        now.
        """
        if self.pool is None:
            return
        profile = dataclasses.replace(
            Profile.from_params(self.pool.params), databases=tuple(databases)
        )
        self.profiles.add(profile)

    # ------------------------------------------------------ warehouse / role

    def action_pick_warehouse(self) -> None:
        self._pick_warehouse()

    @work
    async def _pick_warehouse(self) -> None:
        await self.pick_warehouse()

    async def pick_warehouse(self) -> None:
        """Open the warehouse picker. See `pick_databases` for why this is
        not itself a worker."""
        if self.pool is None:
            return
        cached = self.repo.warehouses(warehouses_scope())
        if cached.is_empty:
            self._request(warehouses_scope(), Priority.INTERACTIVE)
            self.notify("Loading warehouses...", timeout=3)
            return
        current = self.pool.params.warehouse
        chosen = await self.app.push_screen_wait(
            PickerScreen(
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

    def action_pick_role(self) -> None:
        self._pick_role()

    @work
    async def _pick_role(self) -> None:
        await self.pick_role()

    async def pick_role(self) -> None:
        """Open the role picker. See `pick_databases` for why this is not
        itself a worker."""
        if self.pool is None:
            return
        cached = self.repo.roles(roles_scope())
        if cached.is_empty:
            self._request(roles_scope(), Priority.INTERACTIVE)
            self.notify("Loading roles...", timeout=3)
            return
        current = self.pool.params.role
        chosen = await self.app.push_screen_wait(
            PickerScreen(
                [row.name for row in cached.rows],
                title="Role",
                placeholder="type to filter roles",
                current=current,
            )
        )
        if chosen is not None and chosen != current:
            self._switch_role(chosen)

    def prewarm_cache(self) -> None:
        """Warm columns for whatever database the cursor is under, from the
        Connect menu.

        Multiple databases can be selected now, so this acts on the one the
        cursor is currently within -- the same "whatever's under cursor"
        convention `action_refresh` already uses -- rather than requiring
        exactly one database ever be selected.

        Fires the table/view object primes alongside the columns prime,
        rather than the columns prime alone: the columns-prime fallback tiers
        need schema and table/view names to fan out into, and there is no
        automatic trigger for this yet, so a database nobody has browsed this
        session may have none of that cached. Background priority throughout,
        the same tier as the staleness sweep: this can run for a while with
        nobody watching, so it must never jump ahead of interactive/visible
        work or trigger an unattended reconnect.
        """
        database = self.focused_scope().database
        if self.pool is None or database is None:
            self.notify("Select a database first", severity="warning")
            return
        self._request(prime_scope(ObjectKind.TABLE, database), Priority.BACKGROUND)
        self._request(prime_scope(ObjectKind.VIEW, database), Priority.BACKGROUND)
        self._request(columns_prime_scope(database), Priority.BACKGROUND)
        self.notify(f"Pre-warming cache for {database}...", timeout=3)

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

    @work(thread=True)
    def _switch_role(self, role: str) -> None:
        """Apply the switch, then repoint the app at that role's own cache.

        The fetch service is stopped *before* the role itself changes, not
        after: `pool.set_role` returning means every connection already
        carries the new role, so a worker still bound to the old repo that
        drew one of them afterward would write new-role results into the old
        role's cache file -- the exact bleed a file-per-identity cache exists
        to rule out. Stopping first closes that window entirely.
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
        self._request(databases_scope(), Priority.INTERACTIVE)
        self._request(warehouses_scope(), Priority.INTERACTIVE)
        self._request(roles_scope(), Priority.INTERACTIVE)

    def _role_switch_failed(self) -> None:
        assert self.pool is not None
        self.app.restart_fetch(self.pool)
        self.fetch = self.app.fetch

    def action_refresh(self) -> None:
        """Explicitly refresh whatever the cursor is on."""
        node = self.tree.cursor_node
        scope = node.data.scope if node is not None and node.data else None
        if scope is None:
            scope = databases_scope() if not self.databases else None
        if scope is not None:
            self._request(scope, Priority.INTERACTIVE)
