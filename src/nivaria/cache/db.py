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

Table data never goes into the file. A Data tab preview is real rows,
possibly personal data, so it lives in a `MemoryDatabase` instead and is gone
when the app exits -- see that class for why it holds its connection.
"""

from __future__ import annotations

import sqlite3
import threading
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
    files("nivaria.cache").joinpath(name).read_text()
    for name in ("schema.sql", "search.sql")
)

#: Goes up by 1 on any change to either schema script. IF NOT EXISTS means an
#: existing file never picks up a change, not even to a view, so a file stamped
#: with any other version is deleted and rebuilt rather than migrated: nivaria
#: is pre-1.0 and carries no compatibility code. tests/test_cache_db.py pins
#: the scripts' digest to this number so a forgotten bump fails there.
SCHEMA_VERSION = 5


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


#: schema.sql for the freshness ledger, which has to be the same `cache_entry`
#: the file has, since Repository's ledger code reads both. Its mirror tables
#: come along and stay empty, which costs nothing. preview.sql for the rows.
_MEMORY_SCRIPTS: tuple[str, ...] = tuple(
    files("nivaria.cache").joinpath(name).read_text()
    for name in ("schema.sql", "preview.sql")
)


class MemoryDatabase:
    """An in-memory sqlite database for what must never reach the disk.

    The opposite of `Database` on the first choice above: one connection,
    held for this object's lifetime. An in-memory database belongs to the
    connection that created it and vanishes when that connection closes, so
    a connection per call would hand every caller an empty database. That is
    also why a TEMP table on a cache file connection is no alternative: it is
    per connection too, and on disk unless `temp_store` says otherwise.

    The one connection is shared by the event loop and the fetch workers,
    so `connect` serialises them with a lock and there is no WAL. That
    suits what lives here: a few hundred rows, written once per fetch and
    read once per repaint. Reentrant, so a nested `connect` on one thread
    cannot hang the UI. It would commit the outer unit of work early, though,
    so nothing nests one today.
    """

    def __init__(self) -> None:
        self._connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self.connect() as connection:
            # Sorts and indexes build in temporary storage, which is a file
            # by default -- the disk this database exists to stay off.
            connection.execute("PRAGMA temp_store = MEMORY")
            for script in _MEMORY_SCRIPTS:
                connection.executescript(script)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """The held connection, for one unit of work at a time."""
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
