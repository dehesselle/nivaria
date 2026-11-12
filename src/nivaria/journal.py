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

A line is built from names, not from finished text: `Name`, `Scope`,
`ObjectRef` and `SchemaRef` arguments to a `Text` template. The panel shows
them as they are. With `JOURNAL_PSEUDONYMIZE` on, the file shows each as a
numbered placeholder instead (`DB_3.SCH_7.PIPE_12`), so a journal from a real
account can be shared. An f-string would bake the name in before the file's
formatter could replace it, and `tests/test_journal.py` fails on one.

Its level is set here, on the journal's own logger, not left to the root
logger's: `LogViewer` moves root's level with its dropdown, and a record is
checked against the level of the logger it was made on, so the file keeps
what verbose asks for whatever the panel shows.

No Textual here: the fetch service and the pool write to the journal too.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from nivaria import paths
from nivaria.models import (
    CacheState,
    ObjectRef,
    ObjectType,
    SchemaRef,
    Scope,
    ScopeType,
    is_stale,
)
from nivaria.settings import (
    DATA_PREVIEW_ROWS,
    JOURNAL_FILE,
    JOURNAL_PSEUDONYMIZE,
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


# -------------------------------------------------------------------- names


@dataclass(frozen=True)
class Name:
    """A name a pseudonymized file must not show. `kind` is its placeholder's
    prefix: `DB`, `SCH`, an object type, `OBJ`, `ROLE`, `WH` or `ACCT`."""

    kind: str
    value: str


def database(name: str) -> Name:
    return Name("DB", name)


def role(name: str) -> Name:
    return Name("ROLE", name)


def warehouse(name: str) -> Name:
    return Name("WH", name)


def account(name: str) -> Name:
    return Name("ACCT", name)


@dataclass(frozen=True)
class Scrubbed:
    """Text that carries names with no structure to find them by, such as a
    driver's error message. See `_Names.scrub`."""

    text: str


def scrubbed(text: object) -> Scrubbed:
    return Scrubbed(str(text))


@dataclass(frozen=True)
class Text:
    """A journal line, or part of one: a `str.format` template and its
    arguments, kept apart until a handler renders them.

    `str()` gives the real names, which is what the Log panel and
    `record.getMessage()` show. The file's formatter renders through the
    placeholders instead.
    """

    template: str
    args: tuple[object, ...] = ()

    def render(self, names: _Names | None) -> str:
        return self.template.format(*(_render(arg, names) for arg in self.args))

    def __str__(self) -> str:
        return self.render(None)


def text(template: str, *args: object) -> Text:
    return Text(template, args)


def joined(items: Iterable[object], separator: str = ", ") -> Text:
    """`items` one after the other, each rendered like any argument."""
    items = tuple(items)
    return Text(separator.join(["{}"] * len(items)), items)


def _render(arg: object, names: _Names | None) -> object:
    """A name as itself or its placeholder. Anything else is left for the
    template to format, so `{:.1f}` still works on a number."""
    match arg:
        case Text():
            return arg.render(names)
        case Name():
            return arg.value if names is None else names.placeholder(arg)
        case Scrubbed():
            return arg.text if names is None else names.scrub(arg.text)
        case Scope():
            return arg.key if names is None else _scope_key(arg, names)
        case ObjectRef():
            return text(
                "{}.{}.{}",
                database(arg.database),
                Name("SCH", arg.schema),
                Name(arg.type.value, arg.name),
            ).render(names)
        case SchemaRef():
            return text("{}.{}", database(arg.database), Name("SCH", arg.name)).render(
                names
            )
    return arg


def _scope_key(scope: Scope, names: _Names) -> str:
    """The scope's key with its names replaced, built by `Scope.key` itself
    rather than a copy of it."""

    def replaced(kind: str, value: str | None) -> str | None:
        return None if value is None else names.placeholder(Name(kind, value))

    return dataclasses.replace(
        scope,
        database=replaced("DB", scope.database),
        schema=replaced("SCH", scope.schema),
        table=replaced(_table_kind(scope), scope.table),
    ).key


def _table_kind(scope: Scope) -> str:
    """What a scope's `table` slot names. Columns, tags and a preview belong
    to a table or a view, and the scope does not say which."""
    if scope.object_type is not None and scope.type is ScopeType.GRANTS:
        return scope.object_type.value
    if scope.type in (
        ScopeType.PIPE_HISTORY,
        ScopeType.PIPE_ACTIVITY_HISTORY,
    ):
        return ObjectType.PIPE.value
    if scope.type in (ScopeType.TASK_HISTORY, ScopeType.TASK_ACTIVITY_HISTORY):
        return ObjectType.TASK.value
    return "OBJ"


#: Names Snowflake gives every account. They behave unlike the rest, which a
#: journal has to show, and say nothing about whose account it is.
BUILT_INS = frozenset(
    {
        "SNOWFLAKE",
        "SNOWFLAKE_SAMPLE_DATA",
        "INFORMATION_SCHEMA",
        "PUBLIC",
        "ACCOUNTADMIN",
        "ORGADMIN",
        "SECURITYADMIN",
        "SYSADMIN",
        "USERADMIN",
    }
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HOST = re.compile(r"(?:https?://)?[\w.-]*snowflakecomputing\.com\S*|https?://\S+")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


class _Names:
    """The placeholders: each name gets the next number the first time any
    file renders it, and keeps it.

    Numbered, not hashed: a hash of a guessable name (`RAW`, `PROD`) is found
    again by hashing a word list, and a keyed one is hex that is hard to tell
    apart when reading. The number belongs to the string, the prefix to its
    kind, so a table and a pipe of the same name read `TABLE_12` and
    `PIPE_12`.

    Kept in `journal/names.txt`, one `12 PIPE "LOAD_ORDERS"` per line, so the
    numbers hold across sessions and a placeholder is looked up by reading
    the file. Appended to, never rewritten: a sweep can bring hundreds of new
    names, and rewriting the whole file for each would grow with their
    square. What another nivaria appended is read before a number is handed
    out, which leaves two processes only a narrow window to give one number
    twice.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._numbers: dict[str, int] = {}
        self._kinds: dict[str, str] = {}
        self._next = 1
        self._read = 0
        self._lock = threading.Lock()
        self._pattern: re.Pattern[str] | None = None
        self._patterned = 0
        self._upper: dict[str, str] = {}

    def placeholder(self, name: Name) -> str:
        value = name.value
        if value in BUILT_INS:
            return value
        with self._lock:
            number = self._numbers.get(value)
            if number is None:
                number = self._add(name.kind, value)
        return f"{name.kind}_{number}"

    def _add(self, kind: str, value: str) -> int:
        self._catch_up()
        number = self._numbers.get(value)
        if number is not None:
            return number
        number = self._next
        line = f"{number} {kind} {json.dumps(value)}\n".encode()
        # Raised, not swallowed: the file handler gives the journal file up,
        # where a placeholder no file explains would be written otherwise.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("ab") as file:
            file.write(line)
        self._learn(number, kind, value)
        return number

    def _catch_up(self) -> None:
        try:
            with self._path.open("rb") as file:
                file.seek(self._read)
                tail = file.read()
        except FileNotFoundError:
            return
        complete = tail.rfind(b"\n") + 1
        self._read += complete
        for raw in tail[:complete].decode("utf-8", errors="replace").splitlines():
            number, _, rest = raw.partition(" ")
            kind, _, quoted = rest.partition(" ")
            try:
                self._learn(int(number), kind, json.loads(quoted))
            except ValueError:
                continue  # a line torn by a crash; its name is numbered anew

    def _learn(self, number: int, kind: str, value: str) -> None:
        if value in self._numbers:
            return
        self._numbers[value] = number
        self._kinds[value] = kind
        self._upper.setdefault(value.upper(), value)
        self._next = max(self._next, number + 1)

    def scrub(self, message: str) -> str:
        """`message` with every name in it replaced, as well as it can be.

        Known names are replaced wherever they stand, ignoring case, longest
        first. A quoted part that is still unknown becomes `?`: Snowflake
        quotes the names in its errors, and an object in one is usually that
        statement's own, already numbered by its `send` line. E-mail
        addresses and hosts are dropped, which covers a login's user and
        account identifier.
        """
        message = _EMAIL.sub("<email>", message)
        message = _HOST.sub("<host>", message)
        with self._lock:
            self._catch_up()
            pattern = self._known()

        def quoted(match: re.Match[str]) -> str:
            mark, inner = match.group()[0], match.group()[1:-1]
            parts = [self._known_part(part) for part in inner.split(".")]
            return f"{mark}{'.'.join(parts)}{mark}"

        def unquoted(segment: str) -> str:
            if pattern is None:
                return segment
            return pattern.sub(lambda m: self._known_part(m.group()), segment)

        out, at = [], 0
        for match in _QUOTED.finditer(message):
            out += [unquoted(message[at : match.start()]), quoted(match)]
            at = match.end()
        out.append(unquoted(message[at:]))
        return "".join(out)

    def _known_part(self, part: str) -> str:
        if not part or part in BUILT_INS:
            return part
        value = part if part in self._numbers else self._upper.get(part.upper())
        if value is None:
            return "?"
        return f"{self._kinds[value]}_{self._numbers[value]}"

    def _known(self) -> re.Pattern[str] | None:
        if self._patterned != len(self._numbers):
            values = sorted(self._numbers, key=len, reverse=True)
            self._pattern = (
                re.compile(
                    r"(?<![\w$])(?:"
                    + "|".join(re.escape(value) for value in values)
                    + r")(?![\w$])",
                    re.IGNORECASE,
                )
                if values
                else None
            )
            self._patterned = len(self._numbers)
        return self._pattern


_names: _Names | None = None
_names_lock = threading.Lock()


def _placeholders() -> _Names:
    global _names
    with _names_lock:
        if _names is None:
            _names = _Names(paths.cache_dir() / "journal" / "names.txt")
        return _names


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


def subject(target: ObjectRef | SchemaRef | Scope | str) -> Text:
    """What a user line calls the thing on screen: a detail pane's target,
    or a tree node's. A bare string is a database."""
    match target:
        case ObjectRef():
            return text("{} {}", target.type.value.lower(), target)
        case SchemaRef():
            return text("schema {}", target)
        case Scope(type=ScopeType.OBJECTS, object_type=object_type) if (
            object_type is not None
        ):
            assert target.database is not None and target.schema is not None
            return text(
                "{} of {}",
                object_type.plural,
                SchemaRef(target.database, target.schema),
            )
        case Scope():
            return text("{}", target)
        case _:
            return text("database {}", database(target))


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
def user(template: str, *args: object) -> None:
    """Something the user did."""
    _emit(logging.INFO, "user", text(template, *args))


@_guarded
def connection(template: str, *args: object) -> None:
    """Snowflake contact outside the fetch service: logins, USE ROLE/WAREHOUSE."""
    _emit(logging.INFO, "conn", text(template, *args))


@_guarded
def note(template: str, *args: object) -> None:
    """Anything else worth a line, such as a sweep's summary."""
    _emit(logging.INFO, "note", text(template, *args))


@_guarded
def queued(
    scope: Scope,
    reason: Reason,
    detail: str | Text = "",
    *,
    priority: str,
    supersedes: Reason | None = None,
) -> None:
    """A request that queued a job."""
    line = text("{}  {}{} [{}]", scope, reason, _detail(detail), priority)
    if supersedes is not None:
        line = text("{}, supersedes {}", line, supersedes)
    _emit(logging.INFO, "queue", line)


@_guarded
def skipped(scope: Scope, reason: Reason, why: str | Text) -> None:
    """A request that queued nothing. Verbose only."""
    _emit(logging.DEBUG, "skip", text("{}  {}: {}", scope, reason, why))


@_guarded
def dropped(scope: Scope, why: str | Text) -> None:
    """Queued or fetched work that was thrown away. Verbose only."""
    _emit(logging.DEBUG, "drop", text("{}  {}", scope, why))


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
        text("{}  {} [{}]", scope, describe(scope, window), ", ".join(where)),
    )


@_guarded
def done(scope: Scope, rows: int, seconds: float, extra: str = "") -> None:
    """A statement that came back."""
    _emit(
        logging.INFO,
        "done",
        text("{}  {} rows, {:.1f} s{}", scope, rows, seconds, _detail(extra)),
    )


@_guarded
def failed(scope: Scope, message: str) -> None:
    """A statement or its write that failed."""
    first = next((line for line in message.splitlines() if line.strip()), message)
    _emit(logging.WARNING, "fail", text("{}  {}", scope, scrubbed(first)))


def _detail(detail: str | Text) -> str | Text:
    return text(": {}", detail) if detail else ""


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


class _FileFormat(logging.Formatter):
    """Renders a line's names as placeholders while `JOURNAL_PSEUDONYMIZE` is
    on. The same record reaches the Log panel's handler too, so it is a copy
    that is changed."""

    def format(self, record: logging.LogRecord) -> str:
        if config.get(JOURNAL_PSEUDONYMIZE) and isinstance(record.args, tuple):
            names = _placeholders()
            record = copy.copy(record)
            record.args = tuple(
                arg.render(names) if isinstance(arg, Text) else arg
                for arg in record.args
            )
        return super().format(record)


def _emit(level: int, kind: str, line: Text) -> None:
    _sync()
    if _log.isEnabledFor(level):
        _log.log(level, "%-5s %s", kind, line)


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
        _FileFormat("%(asctime)s.%(msecs)03d %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
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
    global _path, _broken, _names
    with _lock:
        _close()
        _path = None
        _broken = False
    with _names_lock:
        _names = None
    _log.setLevel(logging.NOTSET)
