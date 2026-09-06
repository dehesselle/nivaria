"""sqlite plumbing for the cache.

Two deliberate choices:

* **A connection per call, never held.** That keeps the cache usable from the
  Textual event loop and from worker threads alike, without sqlite's
  per-thread connection restriction getting in the way.
* **WAL journalling.** A worker writing a page must not block the UI reading
  the rows it already has; WAL is what makes readers and one writer concurrent.

The cache is an optimisation, never a dependency: if it cannot be opened, the
app still runs, it just has nothing cached.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

#: Every statement in these is CREATE TABLE/VIEW/INDEX IF NOT EXISTS, so
#: applying all of them on every prepare() is as safe as applying them once.
_SCHEMA_SCRIPTS: tuple[str, ...] = tuple(
    files("snowcone.cache").joinpath(name).read_text()
    for name in ("schema.sql", "search.sql", "roles_warehouses.sql")
)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._usable = False

    def prepare(self) -> bool:
        """Create the file's schema. Returns whether the cache is usable."""
        try:
            if isinstance(self.path, Path):
                self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._raw() as connection:
                for script in _SCHEMA_SCRIPTS:
                    connection.executescript(script)
            self._usable = True
        except (sqlite3.Error, OSError):
            self._usable = False
        return self._usable

    @property
    def usable(self) -> bool:
        return self._usable

    @contextmanager
    def _raw(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A short-lived connection for one unit of work."""
        with self._raw() as connection:
            yield connection
