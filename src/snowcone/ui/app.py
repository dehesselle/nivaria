from __future__ import annotations

from textual import work
from textual.app import App

from snowcone import paths
from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.connections import RecentConnections
from snowcone.gateway.gateway import PoolGateway
from snowcone.gateway.pool import ConnectionPool, Connector
from snowcone.service.fetch import FetchService
from snowcone.settings import PAGE_SIZE, POOL_SIZE, config
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

    def on_mount(self) -> None:
        # paths.cache_file() called through the module, not imported by name, so
        # test isolation can redirect it after this module was imported.
        database = Database(paths.cache_file())
        database.prepare()
        self.repo = Repository(database)
        self._start()

    @work
    async def _start(self) -> None:
        """Connect, then hand over to the browser.

        A failed connect re-opens the dialog rather than dropping the user at a
        dead screen, so the loop lives here.
        """
        pool = await self.push_screen_wait(
            ConnectScreen(
                RecentConnections(),
                pool_size=config.get(POOL_SIZE),
                connector=self._connector,
            )
        )
        if pool is None:
            self.exit()
            return
        self.pool = pool
        assert self.repo is not None
        self.fetch = FetchService(
            PoolGateway(pool),
            self.repo,
            workers=pool.size,
            page_size=config.get(PAGE_SIZE),
        )
        self.fetch.start()
        # push, not switch: push_screen_wait has already popped the connect
        # screen, so the stack top here is the default screen and switching
        # would try to pop a result callback it never had.
        await self.push_screen(MainScreen())

    def on_unmount(self) -> None:
        if self.fetch is not None:
            self.fetch.stop()
        if self.pool is not None:
            self.pool.close()


def run_app() -> None:
    SnowconeApp().run()
