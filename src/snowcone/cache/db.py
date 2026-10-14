"""sqlite plumbing for the cache.

Two deliberate choices:

* **A connection per call, never held.** That keeps the cache usable from the
  Textual event loop and from worker threads alike, without sqlite's
  per-thread connection restriction getting in the way.
* **WAL journalling.** A worker writing a page must not block the UI reading
  the rows it already has; WAL is what makes readers and one writer concurrent.

The cache is an optimisation, never a dependency: if it cannot be opened, the
app still runs, it just has nothing cached. For the same reason a file from
another schema version is simply deleted and rebuilt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

#: Every statement in these is CREATE TABLE/VIEW/INDEX IF NOT EXISTS, so
#: applying all of them on every prepare() is as safe as applying them once.
#: Two scripts, not one, because they are different layers: schema.sql is the
#: mirror tables plus the freshness ledger, search.sql the derived view over
#: them (and so has to run second).
_SCHEMA_SCRIPTS: tuple[str, ...] = tuple(
    files("snowcone.cache").joinpath(name).read_text()
    for name in ("schema.sql", "search.sql")
)

#: Goes up by 1 on any change to either schema script. IF NOT EXISTS means an
#: existing file never picks up a change, not even to a view, so a file stamped
#: with any other version is deleted and rebuilt rather than migrated: snowcone
#: is pre-1.0 and carries no compatibility code. tests/test_cache_db.py pins
#: the scripts' digest to this number so a forgotten bump fails there.
SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._usable = False

    def prepare(self) -> bool:
        """Create the file's schema, replacing a file from another schema
        version. Returns whether the cache is usable."""
        try:
            if isinstance(self.path, Path):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self._version() != SCHEMA_VERSION:
                    self._discard(self.path)
            with self._raw() as connection:
                for script in _SCHEMA_SCRIPTS:
                    connection.executescript(script)
                # A PRAGMA takes no bound parameters; the value is a constant.
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._usable = True
        except (sqlite3.Error, OSError):
            self._usable = False
        return self._usable

    def _version(self) -> int:
        with self._raw() as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]

    @staticmethod
    def _discard(path: Path) -> None:
        """Delete the file with its WAL companions, so a stale -wal is never
        replayed into the file that replaces it."""
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)

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
