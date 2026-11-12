# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""A short, timestamped account of what nivaria did and why.

Not the SQL log (`snowflake/sql_log.py`): that one shows exactly what was
sent, and nothing about why. Each line here is one user action, one request
and the trigger behind it, or one statement and how it ended, worded like
docs/data-retrieval.md so a line can be checked against the row of the spec
it should match.

It is an ordinary logger, so the Log panel shows it like any other source,
in memory only. It reaches a file only once the user has switched
`JOURNAL_FILE` on: what someone did in nivaria is not written to disk
unasked.

Its level is set here, on the journal's own logger, not left to the root
logger's: `LogViewer` moves root's level with its dropdown, and a record is
checked against the level of the logger it was made on, so the file keeps
what verbose asks for whatever the panel shows.

No Textual here: the fetch service and the pool write to the journal too.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from nivaria import paths
from nivaria.models import (
    CacheState,
    ObjectRef,
    SchemaRef,
    Scope,
    ScopeType,
    is_stale,
)
from nivaria.settings import (
    DATA_PREVIEW_ROWS,
    JOURNAL_FILE,
    JOURNAL_VERBOSE,
    config,
)

if TYPE_CHECKING:
    from nivaria.cache.repository import Entry, Window

_log = logging.getLogger(__name__)
#: Where trouble with the journal itself is reported. Not the journal's own
#: logger: a file that cannot be written must not be asked to say so.
_trouble = logging.getLogger("nivaria")

#: How many journal files are kept, newest first. One per process, so this is
#: the last that many sessions that had the file switched on.
KEEP_FILES = 20


class Reason(StrEnum):
    """What made nivaria ask for a scope: one per trigger in
    docs/data-retrieval.md, in the words its Triggers table uses."""

    STARTUP = "startup"
    RECONNECT = "reconnect"
    ROLE_SWITCH = "role switch"
    WAREHOUSE_SWITCH = "warehouse switch"
    DATABASE_ADDED = "database added"
    EXPANDED = "expanded"
    CURSOR_SETTLED = "cursor settled"
    TAB_OPENED = "tab opened"
    REPAINT = "repaint"
    SWEEP = "sweep"
    F5 = "f5"
    FULL_REFRESH = "full refresh"
    PICKER = "picker"
    MISSING_OBJECT = "missing object"
    PRIME_FAN_OUT = "prime fan-out"
    #: No caller said. `tests/test_journal.py` fails on any call in `src/`
    #: that leaves it at this, so seeing it in a journal is a bug.
    UNKNOWN = "unknown"


# ------------------------------------------------------------------ wording


def span(seconds: float) -> str:
    """A duration the way a person says it: 45s, 6m12s, 2h05m, 90d."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, rest = divmod(total, 60)
        return f"{minutes}m{rest:02d}s" if rest else f"{minutes}m"
    if total < 86400:
        hours, rest = divmod(total, 3600)
        minutes = rest // 60
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    days, rest = divmod(total, 86400)
    hours = rest // 3600
    return f"{days}d{hours:02d}h" if hours else f"{days}d"


def staleness(entry: Entry, ttl: float, now: float | None = None) -> str:
    """Why `entry` was or was not due, from the same reading `is_stale` makes.

    Stated from the entry rather than re-derived, so the journal says what
    the caller decided on, not what a second calculation thinks it should
    have.
    """
    now = time.time() if now is None else now
    if entry.state is CacheState.ERROR:
        tried = (
            f", last try {span(now - entry.attempted_at)} ago"
            if entry.attempted_at is not None
            else ""
        )
        if entry.fetched_at is None:
            return f"failed, never fetched{tried}"
        return f"failed{tried}, age {span(now - entry.fetched_at)}"
    age = entry.age(now)
    if age is None:
        return "never fetched"
    if ttl <= 0:
        return f"fresh, age {span(age)}, refreshing off"
    word = "stale" if is_stale(age, ttl) else "fresh"
    return f"{word}, age {span(age)}, cache age {span(ttl)}"


def subject(target: ObjectRef | SchemaRef | Scope | str) -> str:
    """What a user line calls the thing on screen: a detail pane's target,
    or a tree node's. A bare string is a database."""
    match target:
        case ObjectRef():
            return f"{target.type.value.lower()} {target.fqn}"
        case SchemaRef():
            return f"schema {target.database}.{target.name}"
        case Scope(type=ScopeType.OBJECTS, object_type=object_type) if (
            object_type is not None
        ):
            return f"{object_type.plural} of {target.database}.{target.schema}"
        case Scope():
            return str(target)
        case _:
            return f"database {target}"


def _window(window: Window | None) -> str:
    if window is None:
        return ""
    if window.full:
        return f", full pass of {span(window.seconds)}"
    return f", window {span(window.seconds)}"


def describe(scope: Scope, window: Window | None = None) -> str:
    """What a scope's statement reads, in docs/data-retrieval.md's words.

    Names are left out: the scope key next to it already carries them.
    """
    match scope.type:
        case ScopeType.DATABASES:
            return "SHOW DATABASES"
        case ScopeType.SCHEMAS:
            return "SHOW SCHEMAS IN DATABASE"
        case ScopeType.OBJECTS:
            assert scope.object_type is not None
            return f"SHOW {scope.object_type.plural.upper()} IN SCHEMA"
        case ScopeType.DATABASE_PRIME:
            assert scope.object_type is not None
            return f"SHOW {scope.object_type.plural.upper()} IN DATABASE"
        case ScopeType.COLUMNS:
            return "SHOW COLUMNS IN TABLE"
        case ScopeType.TABLE_STATS:
            return "INFORMATION_SCHEMA.TABLES, base tables"
        case ScopeType.VIEW_STATS:
            return "INFORMATION_SCHEMA.TABLES, views"
        case ScopeType.MATERIALIZED_VIEW_STATS:
            return "SHOW MATERIALIZED VIEWS IN SCHEMA"
        case ScopeType.STAGE_STATS:
            return "INFORMATION_SCHEMA.STAGES"
        case ScopeType.PIPE_ACTIVITY:
            return f"ACCOUNT_USAGE.COPY_HISTORY, latest row per pipe{_window(window)}"
        case ScopeType.PIPE_ACTIVITY_HISTORY:
            return f"ACCOUNT_USAGE.COPY_HISTORY, one pipe{_window(window)}"
        case ScopeType.PIPE_HISTORY:
            return "INFORMATION_SCHEMA.COPY_HISTORY"
        case ScopeType.PIPE_STATUS:
            return "SYSTEM$PIPE_STATUS"
        case ScopeType.TASK_ACTIVITY:
            return f"ACCOUNT_USAGE.TASK_HISTORY, latest row per task{_window(window)}"
        case ScopeType.TASK_ACTIVITY_HISTORY:
            return f"ACCOUNT_USAGE.TASK_HISTORY, one task{_window(window)}"
        case ScopeType.TASK_HISTORY:
            return "INFORMATION_SCHEMA.TASK_HISTORY"
        case ScopeType.GRANTS:
            if scope.table is not None:
                assert scope.object_type is not None
                return f"SHOW GRANTS ON {scope.object_type.value}"
            if scope.schema is not None:
                return "SHOW GRANTS ON SCHEMA"
            return "SHOW GRANTS ON DATABASE"
        case ScopeType.TAG_REFERENCES:
            return "INFORMATION_SCHEMA.TAG_REFERENCES"
        case ScopeType.DATABASE_ROLES:
            return "SHOW DATABASE ROLES"
        case ScopeType.COLUMN_TAG_REFERENCES:
            return "INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS"
        case ScopeType.DATA_PREVIEW:
            return f"SELECT * ... LIMIT {config.get(DATA_PREVIEW_ROWS)}"
        case ScopeType.WAREHOUSES:
            return "SHOW WAREHOUSES"
        case ScopeType.ROLES:
            return "SHOW ROLES"
    raise AssertionError(f"journal.describe has no case for {scope.type}")


# ----------------------------------------------------------------- emitters


def _guarded[**P](emitter: Callable[P, None]) -> Callable[P, None]:
    """Never let a journal line break what it describes: a fetch worker or a
    key press is the caller."""

    @functools.wraps(emitter)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            emitter(*args, **kwargs)
        except Exception:  # noqa: BLE001
            _trouble.exception("the journal failed to write a line")

    return wrapper


@_guarded
def user(text: str) -> None:
    """Something the user did."""
    _emit(logging.INFO, "user", text)


@_guarded
def connection(text: str) -> None:
    """Snowflake contact outside the fetch service: logins, USE ROLE/WAREHOUSE."""
    _emit(logging.INFO, "conn", text)


@_guarded
def note(text: str) -> None:
    """Anything else worth a line, such as a sweep's summary."""
    _emit(logging.INFO, "note", text)


@_guarded
def queued(
    scope: Scope,
    reason: Reason,
    detail: str = "",
    *,
    priority: str,
    supersedes: Reason | None = None,
) -> None:
    """A request that queued a job."""
    text = f"{scope}  {reason}{_detail(detail)} [{priority}]"
    if supersedes is not None:
        text += f", supersedes {supersedes}"
    _emit(logging.INFO, "queue", text)


@_guarded
def skipped(scope: Scope, reason: Reason, why: str) -> None:
    """A request that queued nothing. Verbose only."""
    _emit(logging.DEBUG, "skip", f"{scope}  {reason}: {why}")


@_guarded
def dropped(scope: Scope, why: str) -> None:
    """Queued or fetched work that was thrown away. Verbose only."""
    _emit(logging.DEBUG, "drop", f"{scope}  {why}")


@_guarded
def sent(
    scope: Scope,
    reason: Reason,
    *,
    page: int = 0,
    window: Window | None = None,
    reconnect: bool = True,
) -> None:
    """A statement about to go to Snowflake."""
    where = [str(reason)]
    if page:
        where.append(f"page {page + 1}")
    where.append(threading.current_thread().name)
    if not reconnect:
        where.append("no reconnect")
    _emit(
        logging.INFO,
        "send",
        f"{scope}  {describe(scope, window)} [{', '.join(where)}]",
    )


@_guarded
def done(scope: Scope, rows: int, seconds: float, extra: str = "") -> None:
    """A statement that came back."""
    _emit(
        logging.INFO,
        "done",
        f"{scope}  {rows} rows, {seconds:.1f} s{_detail(extra)}",
    )


@_guarded
def failed(scope: Scope, message: str) -> None:
    """A statement or its write that failed."""
    first = next((line for line in message.splitlines() if line.strip()), message)
    _emit(logging.WARNING, "fail", f"{scope}  {first}")


def _detail(text: str) -> str:
    return f": {text}" if text else ""


# --------------------------------------------------------------------- file

_lock = threading.Lock()
_handler: logging.Handler | None = None
#: The file for this process, chosen the first time the file is switched
#: on, so switching it off and on again appends to the same one.
_path: Path | None = None
#: Set once the file could not be written. It stays off for the session
#: rather than failing, and warning, on every line.
_broken = False


class _JournalFile(logging.FileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The default prints a traceback to stderr, right across the TUI.
        _give_up(self.baseFilename)


def _emit(level: int, kind: str, text: str) -> None:
    _sync()
    if _log.isEnabledFor(level):
        _log.log(level, "%-5s %s", kind, text)


def _sync() -> None:
    """Apply the journal's settings, which are read at the point of use like
    every other setting so a change needs no restart."""
    level = logging.DEBUG if config.get(JOURNAL_VERBOSE) else logging.INFO
    if _log.level != level:
        _log.setLevel(level)
    want = config.get(JOURNAL_FILE) and not _broken
    if want == (_handler is not None):
        return
    with _lock:
        if want and _handler is None:
            _open()
        elif not want and _handler is not None:
            _close()


def _open() -> None:
    global _handler, _path
    directory = paths.cache_dir() / "journal"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if _path is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            _path = directory / f"journal-{stamp}-{os.getpid()}.log"
            _prune(directory, keep=_path)
        handler = _JournalFile(_path, encoding="utf-8", delay=True)
    except OSError:
        _give_up(str(directory))
        return
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    _log.addHandler(handler)
    _handler = handler


def _close() -> None:
    global _handler
    if _handler is None:
        return
    _log.removeHandler(_handler)
    _handler.close()
    _handler = None


def _prune(directory: Path, *, keep: Path) -> None:
    """Delete all but the newest `KEEP_FILES` journals, `keep` counted."""
    older = sorted(
        (path for path in directory.glob("journal-*.log") if path != keep),
        key=lambda path: path.name,
        reverse=True,
    )
    for path in older[KEEP_FILES - 1 :]:
        try:
            path.unlink()
        except OSError:
            pass  # in use by another nivaria, or already gone


def _give_up(where: str) -> None:
    global _broken
    if _broken:
        return
    _broken = True
    _trouble.warning("the journal cannot be written to %s; not writing it", where)
    if _handler is not None:
        # Silenced rather than removed: this can run inside the handler's own
        # emit, while logging walks the handler list. The next line's _sync
        # closes it.
        _handler.setLevel(logging.CRITICAL + 1)


def reset() -> None:
    """Close the file and forget this process's journal. For tests, which
    each point the cache directory somewhere else."""
    global _path, _broken
    with _lock:
        _close()
        _path = None
        _broken = False
    _log.setLevel(logging.NOTSET)
