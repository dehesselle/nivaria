# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import asyncio
from time import monotonic

from tcoda.chords import ChordMixin
from tcoda.terminal_title import TerminalTitleMixin
from textual import events, work
from textual.app import App

from nivaria import journal, paths
from nivaria.cache.db import Database, MemoryDatabase
from nivaria.cache.repository import Repository
from nivaria.export import EXPORT_DELIVERY
from nivaria.journal import Reason
from nivaria.models import ScopeType
from nivaria.profiles import ProfileStore
from nivaria.service.fetch import FetchService
from nivaria.service.jobs import Priority
from nivaria.settings import POOL_SIZE, config
from nivaria.snowflake.gateway import PoolGateway
from nivaria.snowflake.pool import LOGIN_TIMED_OUT, ConnectionPool, Connector
from nivaria.ui.messages import CacheUpdated
from nivaria.ui.screens.closing import ClosingDialog
from nivaria.ui.screens.connect import ConnectScreen
from nivaria.ui.screens.main import MainScreen

#: How long a "no longer exists" notice stays up, and so how long an
#: identical one is suppressed.
_MISSING_TIMEOUT = 5.0


class NivariaApp(ChordMixin, TerminalTitleMixin, App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "nivaria"
    TERMINAL_TITLE = TITLE

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
        #: When each "no longer exists" notice was last shown, by text.
        self._notices: dict[str, float] = {}
        #: Set while quit waits for running statements; a second quit then
        #: exits without waiting.
        self._quitting = False

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
        pool = await self.push_screen_wait(self._connect_dialog())
        if pool is None:
            self.exit()
            return
        self.pool = pool
        _journal_session("connected", pool)
        self.switch_repo(pool)
        # push, not switch: push_screen_wait has already popped the connect
        # screen, so the stack top here is the default screen and switching
        # would try to pop a result callback it never had.
        screen = MainScreen(self.repo, self.fetch, pool, self.profiles)
        await self.push_screen(screen)
        self._restore_databases(screen, pool)

    async def open_connect(self) -> None:
        """Reopen the connect dialog and swap in the resulting pool.

        Invoked from the Account > Connect chord once already browsing. A
        cancelled dialog leaves the existing connection untouched.

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
        interrupted mid-query (see docs/DESIGN.md), so it can genuinely block for
        a couple of seconds. Both it and `ConnectionPool.close()` run off the
        event-loop thread so the UI can still repaint, behind a small
        "please wait" dialog that makes the wait visible instead of the app
        appearing to hang.
        """
        pool = await self.push_screen_wait(self._connect_dialog())
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
        _journal_session("reconnected", pool)
        self.switch_repo(pool)
        if isinstance(self.screen, MainScreen):
            self.screen.pool = pool
            self.screen.fetch = self.fetch
            # A reconnect can just as well land on a different account or
            # role as the last one -- same reasoning as a role switch, so the
            # same reset applies: whatever was on screen belonged to the old
            # identity's cache.
            self.screen.set_repo(self.repo)
            self.screen.prefetch_session_lists(Reason.RECONNECT)
            self._restore_databases(self.screen, pool)

    def _connect_dialog(self) -> ConnectScreen:
        return ConnectScreen(
            self.profiles,
            pool_size=config.get(POOL_SIZE),
            connector=self._connector,
        )

    def _restore_databases(self, screen: MainScreen, pool: ConnectionPool) -> None:
        """Select whatever databases this identity last browsed.

        A picker confirmation is what *records* a selection
        (MainScreen._remember_databases), not this; re-recording an unchanged
        restore would also incorrectly refresh role/warehouse on every plain
        startup.
        """
        profile = self.profiles.find(pool.params)
        if profile is not None and profile.databases:
            screen.set_databases(list(profile.databases))

    def _open_repo(self, pool: ConnectionPool) -> Repository:
        """The cache for whatever identity `pool.params` currently describes.
        See `paths.cache_file` for why that identity is the whole key.

        The cache is an optimisation, never a dependency, so one that cannot
        be opened costs what it would have kept, not the session.
        """
        path = paths.cache_file(pool.params.account, pool.params.role)
        database = Database(path)
        if database.prepare():
            return Repository(database)
        self.notify(
            f"Cannot open the cache at {path}. Nothing will be kept after this "
            "session.",
            severity="warning",
            timeout=8,
        )
        return Repository(MemoryDatabase())

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
        # Per-account settings (SNOWSIGHT_URL) resolve against this. The
        # account alone, not the cache file's (account, role): a URL does not
        # change with the role.
        config.current_instance = pool.params.account.casefold()
        self.repo = self._open_repo(pool)
        self.restart_fetch(pool)

    def _make_fetch(self, pool: ConnectionPool) -> FetchService:
        assert self.repo is not None
        return FetchService(
            PoolGateway(pool),
            self.repo,
            workers=pool.size,
            notify=self._cache_updated,
            on_error=self._fetch_failed,
        )

    def _cache_updated(self, scope, complete: bool, priority: Priority) -> None:
        """Called on a fetch worker thread; hop to the event loop.

        The message goes to the screen, which routes it to the one widget
        showing that scope. Nothing here touches a widget directly.
        """
        self.call_from_thread(self._post_cache_updated, scope, complete, priority)

    def _post_cache_updated(self, scope, complete: bool, priority: Priority) -> None:
        # A worker started before quit can still be mid-query when shutdown
        # pops every screen (Textual's own _close_all runs before this app's
        # on_unmount), so `self.screen` is not safe to touch unconditionally
        # here -- it raises ScreenStackError once nothing is left on the
        # stack, crashing the app on the way out instead of exiting quietly.
        if not self.screen_stack:
            return
        screen = self.screen
        if isinstance(screen, MainScreen):
            screen.post_message(CacheUpdated(scope, complete, priority))

    def _fetch_failed(self, scope, message: str, missing: bool) -> None:
        """Called on a fetch worker thread; hop to the event loop."""
        self.call_from_thread(self._report_fetch_failure, scope, message, missing)

    def _report_fetch_failure(self, scope, message: str, missing: bool) -> None:
        if missing and scope.type is ScopeType.PIPE_HISTORY:
            # The pipe's target table, not the pipe: a lasting state retried
            # every cache age, which the History note reports. A toast would
            # come back each time, promising a refresh that changes nothing.
            return
        if message == LOGIN_TIMED_OUT:
            # One unanswered prompt fails every scope waiting on it, and what
            # needs saying is about the login, not about any of them.
            self._notify_once(
                f"Login timed out: {message}. Press F5 to be asked again.", 8.0
            )
            return
        if not missing:
            self.notify(
                f"Could not load {scope}: {message}", severity="error", timeout=8
            )
            return
        # Something dropped elsewhere, which the fetch service is already
        # clearing out of the tree -- not an error the user has to act on.
        # Snowflake's own last line names the object ("Schema 'DB.S' does not
        # exist or not authorized."), which reads better than our scope key.
        # One visit fails several scopes over the same object at once (a
        # schema's Grants and Tags), so a repeat of the same text is dropped
        # while the first toast is still up.
        lines = [line for line in message.splitlines() if line.strip()]
        text = f"{lines[-1] if lines else message} Refreshing."
        self._notify_once(text, _MISSING_TIMEOUT)

    def _notify_once(self, text: str, timeout: float) -> None:
        """A warning, unless the same text is still up from a moment ago."""
        now = monotonic()
        if now - self._notices.get(text, float("-inf")) < timeout:
            return
        self._notices[text] = now
        self.notify(text, severity="warning", timeout=timeout)

    # Here rather than on the screen that asks for the export: these events go
    # to the app and do not bubble. `markup=False`, because a path can hold `[`.
    def on_delivery_complete(self, event: events.DeliveryComplete) -> None:
        if event.name == EXPORT_DELIVERY:
            self.notify(f"Saved {event.path}", title="Export", timeout=8, markup=False)

    def on_delivery_failed(self, event: events.DeliveryFailed) -> None:
        if event.name == EXPORT_DELIVERY:
            self.notify(
                f"Could not save: {event.exception}",
                title="Export",
                severity="error",
                timeout=8,
                markup=False,
            )

    async def action_quit(self) -> None:
        """Wait for statements in flight before exiting; quit again to stop
        waiting.

        A fetch worker cannot be interrupted mid-query (see docs/DESIGN.md), so
        closing every connection cleanly means waiting for it.
        """
        fetch = self.fetch
        if self._quitting or fetch is None or not fetch.running_count:
            journal.user("quit")
            self.exit()
            return
        journal.user(f"quit, waiting for {fetch.running_count} statements")
        self._quitting = True
        self._wait_for_jobs_then_exit(fetch)

    # A worker, not awaited in the action: an action blocks the app's message
    # queue until it returns, so the second Ctrl+Q would never be handled.
    @work(group="quit")
    async def _wait_for_jobs_then_exit(self, fetch: FetchService) -> None:
        self.push_screen(
            ClosingDialog(
                "Waiting for background jobs to finish...",
                hint="Ctrl+Q again to quit now",
            )
        )
        fetch.stop(timeout=0)
        # Polled, not asyncio.to_thread(fetch.stop, None): asyncio waits for
        # its executor threads on the way out, so a forced quit would still
        # sit out the query.
        while fetch.running_count:
            await asyncio.sleep(0.1)
        self.exit()

    def call_from_thread(self, callback, *args, **kwargs):
        # Fetch workers, connecting and role/warehouse switches all run on
        # threads that cannot be interrupted, so one can finish after quit.
        # Its result is for a UI that no longer exists, and Textual would
        # raise "App is not running" in that thread.
        if not self.is_running:
            return None
        return super().call_from_thread(callback, *args, **kwargs)

    def on_unmount(self) -> None:
        if self.fetch is not None:
            self.fetch.stop()
        if self.pool is not None:
            self.pool.close()


def _journal_session(what: str, pool: ConnectionPool) -> None:
    params = pool.params
    journal.connection(
        f"{what}: account {pool.account_name}, role {params.role or 'none'}, "
        f"warehouse {params.warehouse or 'none'}, {pool.size} connections"
    )


def run_app() -> None:
    NivariaApp().run()
