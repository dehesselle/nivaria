from __future__ import annotations

import asyncio

from textual import work
from textual.app import App

from snowcone import paths
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.gateway.gateway import PoolGateway
from snowcone.gateway.pool import ConnectionPool, Connector
from snowcone.profiles import Profile, ProfileStore
from snowcone.service.fetch import FetchService
from snowcone.settings import PAGE_SIZE, POOL_SIZE, config
from snowcone.ui.messages import CacheUpdated
from snowcone.ui.screens.closing import ClosingDialog
from snowcone.ui.screens.connect import ConnectScreen
from snowcone.ui.screens.main import MainScreen


class SnowconeApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "snowcone"

    def __init__(self, *, connector: Connector | None = None) -> None:
        super().__init__()
        self._connector = connector
        self.pool: ConnectionPool | None = None
        self.fetch: FetchService | None = None
        self.repo: Repository | None = None
        # One shared instance -- both ConnectScreen and MainScreen read and
        # write through it, so there is a single source of truth object for
        # the app's lifetime, not several independently-reloading views onto
        # the same file.
        self.profiles = ProfileStore()

    def on_mount(self) -> None:
        self._start()

    @work
    async def _start(self) -> None:
        """Connect, then hand over to the browser.

        A failed connect re-opens the dialog rather than dropping the user at a
        dead screen, so the loop lives here. The cache is not opened until a
        pool exists: which file that is depends on the connection's identity
        (see `paths.cache_file`), which is not known any earlier than this.
        """
        pool = await self.push_screen_wait(
            ConnectScreen(
                self.profiles,
                pool_size=config.get(POOL_SIZE),
                connector=self._connector,
            )
        )
        if pool is None:
            self.exit()
            return
        self.pool = pool
        self.switch_repo(pool)
        # push, not switch: push_screen_wait has already popped the connect
        # screen, so the stack top here is the default screen and switching
        # would try to pop a result callback it never had.
        screen = MainScreen(self.repo, self.fetch, pool, self.profiles)
        await self.push_screen(screen)
        # Restore whatever databases were selected the last time this
        # identity was browsed -- a picker confirmation is what *records* a
        # selection (MainScreen._remember_databases), not this; re-recording
        # an unchanged restore would also incorrectly refresh role/warehouse
        # on every plain startup.
        profile = self._matching_profile(pool)
        if profile is not None and profile.databases:
            screen.set_databases(list(profile.databases))

    async def open_connect(self) -> None:
        """Reopen the connect dialog and swap in the resulting pool.

        Invoked from the "Connect > Open..." menu item once already browsing.
        A cancelled dialog leaves the existing connection untouched.

        The old fetch service and pool are stopped *before* anything about
        the new one becomes visible to `MainScreen` -- the same discipline
        `MainScreen._switch_role` uses for a role switch, and for the same
        reason: the old `FetchService` keeps completing jobs and posting
        `ScopeNeeded` for the old identity for as long as this dialog is
        open, and a message still in flight when the screen is repointed
        would otherwise be resolved against the *new* fetch while still
        naming the *old* identity's scope -- firing SQL for a database that
        may not even exist in the new account. By the time this runs, `pool`
        is already known good (`ConnectScreen` only ever returns one once
        `connect_all`/growth succeeded), so there is no failure case left for
        stopping the old state first to protect against.

        `FetchService.stop()` joins worker threads that cannot be
        interrupted mid-query (see DESIGN.md), so it can genuinely block for
        a couple of seconds. Both it and `ConnectionPool.close()` run off the
        event-loop thread so the UI can still repaint, behind a small
        "please wait" dialog that makes the wait visible instead of the app
        appearing to hang.
        """
        pool = await self.push_screen_wait(
            ConnectScreen(
                self.profiles,
                pool_size=config.get(POOL_SIZE),
                connector=self._connector,
            )
        )
        if pool is None:
            return
        old_fetch, old_pool = self.fetch, self.pool
        closing = ClosingDialog()
        self.push_screen(closing)
        if old_fetch is not None:
            await asyncio.to_thread(old_fetch.stop)
        if old_pool is not None:
            await asyncio.to_thread(old_pool.close)
        closing.dismiss()
        self.pool = pool
        self.switch_repo(pool)
        if isinstance(self.screen, MainScreen):
            self.screen.pool = pool
            self.screen.fetch = self.fetch
            # A reconnect can just as well land on a different account or
            # role as the last one -- same reasoning as a role switch, so the
            # same reset applies: whatever was on screen belonged to the old
            # identity's cache.
            self.screen.set_repo(self.repo)
            profile = self._matching_profile(pool)
            if profile is not None and profile.databases:
                self.screen.set_databases(list(profile.databases))

    def _matching_profile(self, pool: ConnectionPool) -> Profile | None:
        """The stored profile for `pool`'s identity, if any -- used to
        restore a previously remembered database selection."""
        fresh = Profile.from_params(pool.params)
        return next(
            (p for p in self.profiles.load() if p.identity == fresh.identity), None
        )

    def _open_repo(self, pool: ConnectionPool) -> Repository:
        """The cache for whatever identity `pool.params` currently describes.
        See `paths.cache_file` for why that identity is the whole key."""
        database = Database(paths.cache_file(pool.params.account, pool.params.role))
        database.prepare()
        return Repository(database)

    def restart_fetch(self, pool: ConnectionPool) -> None:
        """A fresh FetchService against the current `self.repo` and `pool`.

        Used to get fetching going again after it was stopped without the
        identity actually changing -- e.g. a role switch that failed after
        the fetch service was pre-emptively stopped to keep it from racing
        the switch (see `MainScreen._switch_role`).
        """
        self.fetch = self._make_fetch(pool)
        self.fetch.start()

    def switch_repo(self, pool: ConnectionPool) -> None:
        """Point at the cache for whatever identity `pool.params` now
        describes, and restart fetch against it. Used after a fresh connect, a
        reconnect, and a successful role switch -- anywhere the identity
        behind `pool` may have just changed.
        """
        self.repo = self._open_repo(pool)
        self.restart_fetch(pool)

    def _make_fetch(self, pool: ConnectionPool) -> FetchService:
        assert self.repo is not None
        return FetchService(
            PoolGateway(pool),
            self.repo,
            workers=pool.size,
            page_size=config.get(PAGE_SIZE),
            notify=self._cache_updated,
            on_error=self._fetch_failed,
        )

    def _cache_updated(self, scope, complete: bool) -> None:
        """Called on a fetch worker thread; hop to the event loop.

        The message goes to the screen, which routes it to the one widget
        showing that scope. Nothing here touches a widget directly.
        """
        self.call_from_thread(self._post_cache_updated, scope, complete)

    def _post_cache_updated(self, scope, complete: bool) -> None:
        # A worker started before quit can still be mid-query when shutdown
        # pops every screen (Textual's own _close_all runs before this app's
        # on_unmount), so `self.screen` is not safe to touch unconditionally
        # here -- it raises ScreenStackError once nothing is left on the
        # stack, crashing the app on the way out instead of exiting quietly.
        if not self.screen_stack:
            return
        screen = self.screen
        if isinstance(screen, MainScreen):
            screen.post_message(CacheUpdated(scope, complete))

    def _fetch_failed(self, scope, message: str) -> None:
        self.call_from_thread(
            self.notify,
            f"Could not load {scope}: {message}",
            severity="error",
            timeout=8,
        )

    def on_unmount(self) -> None:
        if self.fetch is not None:
            self.fetch.stop()
        if self.pool is not None:
            self.pool.close()


def run_app() -> None:
    SnowconeApp().run()
