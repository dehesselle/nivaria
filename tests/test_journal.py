# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The journal on its own: its wording, its file, and its settings."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nivaria import journal, paths
from nivaria.cache.repository import Entry, Window
from nivaria.models import (
    CacheState,
    ObjectRef,
    ObjectType,
    Scope,
    ScopeType,
    columns_scope,
    object_grants_scope,
    pipe_activity_scope,
    pipe_history_scope,
)
from nivaria.settings import (
    JOURNAL_FILE,
    JOURNAL_PSEUDONYMIZE,
    JOURNAL_VERBOSE,
    config,
)

NOW = 1_000_000.0


def _entry(**fields) -> Entry:
    defaults = {
        "scope_key": "pipe_activity:DB.S",
        "state": CacheState.COMPLETE,
        "generation": 1,
        "fetched_at": None,
        "row_count": 0,
        "error": None,
    }
    return Entry(**{**defaults, **fields})


def _journal_dir() -> Path:
    return paths.cache_dir() / "journal"


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "nivaria.journal"
    ]


# ------------------------------------------------------------------ wording


@pytest.mark.parametrize(
    ("seconds", "said"),
    [
        (0, "0s"),
        (45, "45s"),
        (300, "5m"),
        (372, "6m12s"),
        (7200, "2h"),
        (7500, "2h05m"),
        (90 * 86400, "90d"),
        (86400 + 4 * 3600, "1d04h"),
    ],
)
def test_a_span_reads_the_way_a_person_says_it(seconds: float, said: str) -> None:
    assert journal.span(seconds) == said


def test_staleness_says_never_fetched() -> None:
    assert journal.staleness(_entry(), 300, NOW) == "never fetched"


def test_staleness_says_stale_with_the_age_and_the_cache_age() -> None:
    entry = _entry(fetched_at=NOW - 372)
    assert journal.staleness(entry, 300, NOW) == "stale, age 6m12s, cache age 5m"


def test_staleness_says_fresh_when_the_scope_was_not_due() -> None:
    entry = _entry(fetched_at=NOW - 63)
    assert journal.staleness(entry, 300, NOW) == "fresh, age 1m03s, cache age 5m"


def test_staleness_says_when_refreshing_is_off() -> None:
    entry = _entry(fetched_at=NOW - 63)
    assert journal.staleness(entry, 0, NOW) == "fresh, age 1m03s, refreshing off"


def test_staleness_says_a_scope_failed_and_when_it_was_tried() -> None:
    entry = _entry(state=CacheState.ERROR, fetched_at=NOW - 600, attempted_at=NOW - 240)
    assert journal.staleness(entry, 300, NOW) == "failed, last try 4m ago, age 10m"


def test_staleness_says_a_first_fetch_failed() -> None:
    entry = _entry(state=CacheState.ERROR, attempted_at=NOW - 240)
    assert journal.staleness(entry, 300, NOW) == (
        "failed, never fetched, last try 4m ago"
    )


@pytest.mark.parametrize("scope_type", list(ScopeType))
def test_every_scope_type_has_a_description(scope_type: ScopeType) -> None:
    scope = Scope(
        scope_type,
        database="DB",
        schema="S",
        object_type=ObjectType.TABLE,
        table="T",
    )
    assert journal.describe(scope)


def test_a_log_scope_says_whether_it_is_a_window_or_a_full_pass() -> None:
    scope = pipe_activity_scope("DB", "S")
    assert journal.describe(scope, Window(seconds=4 * 3600, full=False)).endswith(
        "window 4h"
    )
    assert journal.describe(scope, Window(seconds=90 * 86400, full=True)).endswith(
        "full pass of 90d"
    )


# --------------------------------------------------------------- the panel


def test_a_line_reaches_the_log_panel_with_the_file_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    journal.user("cursor rested on pipe DB.S.P")
    assert _lines(caplog) == ["user  cursor rested on pipe DB.S.P"]
    assert not _journal_dir().exists()


def test_what_sent_nothing_is_journalled_only_when_verbose(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scope = pipe_activity_scope("DB", "S")
    journal.skipped(scope, journal.Reason.SWEEP, "already running")
    assert _lines(caplog) == []

    config.set(JOURNAL_VERBOSE, True)
    journal.skipped(scope, journal.Reason.SWEEP, "already running")
    assert _lines(caplog) == ["skip  pipe_activity:DB.S  sweep: already running"]


def test_the_level_holds_whatever_the_root_logger_is_set_to(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LogViewer moves the root level with its dropdown; the journal must not
    follow it."""
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.CRITICAL)
    try:
        journal.user("pressed F5")
    finally:
        root.setLevel(before)
    assert _lines(caplog) == ["user  pressed F5"]


def test_a_failing_line_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(journal, "describe", broken)
    journal.sent(pipe_activity_scope("DB", "S"), journal.Reason.F5)
    assert "the journal failed to write a line" in caplog.text


# ---------------------------------------------------------------- the file


def _files() -> list[Path]:
    return sorted(_journal_dir().glob("journal-*.log"))


def test_the_file_holds_timestamped_journal_lines_only() -> None:
    config.set(JOURNAL_FILE, True)
    journal.user("pressed F5")
    logging.getLogger("nivaria.fetch").warning("not a journal line")

    [file] = _files()
    [line] = file.read_text(encoding="utf-8").splitlines()
    date, clock, rest = line.split(" ", 2)
    assert len(date) == 10 and len(clock) == 12  # 2026-11-12 14:03:22.481
    assert rest == "user  pressed F5"


def test_switching_the_file_off_stops_it_and_on_again_appends() -> None:
    config.set(JOURNAL_FILE, True)
    journal.user("one")
    config.set(JOURNAL_FILE, False)
    journal.user("two")
    config.set(JOURNAL_FILE, True)
    journal.user("three")

    [file] = _files()
    text = file.read_text(encoding="utf-8")
    assert "one" in text and "two" not in text and "three" in text


def test_only_the_newest_files_are_kept() -> None:
    directory = _journal_dir()
    directory.mkdir(parents=True)
    for day in range(1, 31):
        (directory / f"journal-202610{day:02d}-120000-1.log").write_text("")

    config.set(JOURNAL_FILE, True)
    journal.user("now")

    kept = _files()
    assert len(kept) == journal.KEEP_FILES
    # The 19 newest old ones survive, next to this session's own.
    assert kept[0].name == "journal-20261012-120000-1.log"


def test_a_file_that_cannot_be_written_is_given_up_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = paths.cache_dir()
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("a file where the directory should be")
    config.set(JOURNAL_FILE, True)

    journal.user("one")
    journal.user("two")

    assert _lines(caplog) == ["user  one", "user  two"]
    warnings = [r for r in caplog.records if "cannot be written" in r.getMessage()]
    assert len(warnings) == 1


# ----------------------------------------------------------- placeholders


def _pseudonymized() -> list[str]:
    """The message part of every line in this session's file."""
    [file] = _files()
    return [
        line.split(" ", 2)[2] for line in file.read_text(encoding="utf-8").splitlines()
    ]


def _names_file() -> Path:
    return _journal_dir() / "names.txt"


@pytest.fixture
def pseudonymized() -> None:
    config.set(JOURNAL_FILE, True)
    config.set(JOURNAL_PSEUDONYMIZE, True)


def test_the_file_shows_placeholders_and_the_panel_the_names(
    pseudonymized: None, caplog: pytest.LogCaptureFixture
) -> None:
    ref = ObjectRef(ObjectType.PIPE, "SALES", "RAW", "LOAD_ORDERS")
    journal.user("cursor rested on {}", journal.subject(ref))
    journal.sent(pipe_history_scope("SALES", "RAW", "LOAD_ORDERS"), journal.Reason.F5)

    assert _lines(caplog)[0] == "user  cursor rested on pipe SALES.RAW.LOAD_ORDERS"
    user, send = _pseudonymized()
    assert user == "user  cursor rested on pipe DB_1.SCH_2.PIPE_3"
    assert send.startswith("send  pipe_history:DB_1.SCH_2.PIPE_3  ")
    text = "\n".join(_pseudonymized())
    assert not any(name in text for name in ("SALES", "RAW", "LOAD_ORDERS"))


def test_a_placeholder_number_belongs_to_the_string_whatever_its_kind(
    pseudonymized: None,
) -> None:
    """A table and a pipe may share a name, and the journal has to show it."""
    table = ObjectRef(ObjectType.TABLE, "DB", "S", "ORDERS")
    pipe = ObjectRef(ObjectType.PIPE, "DB", "S", "ORDERS")
    journal.queued(object_grants_scope(table), journal.Reason.TAB_OPENED, priority="x")
    journal.queued(object_grants_scope(pipe), journal.Reason.TAB_OPENED, priority="x")

    first, second = _pseudonymized()
    assert first.startswith("queue grants:tables:DB_1.SCH_2.TABLE_3  ")
    assert second.startswith("queue grants:pipes:DB_1.SCH_2.PIPE_3  ")


def test_columns_name_their_object_without_saying_table_or_view(
    pseudonymized: None,
) -> None:
    journal.done(columns_scope("DB", "S", "V"), 3, 0.25)
    assert _pseudonymized() == ["done  columns:DB_1.SCH_2.OBJ_3  3 rows, 0.2 s"]


def test_placeholders_hold_across_sessions(pseudonymized: None) -> None:
    journal.user("role {} -> {}", journal.role("ANALYST"), journal.role("LOADER"))
    journal.reset()
    for file in _files():
        file.unlink()
    journal.user("role {} -> {}", journal.role("LOADER"), journal.role("AUDITOR"))

    assert _pseudonymized() == ["user  role ROLE_2 -> ROLE_3"]
    assert _names_file().read_text(encoding="utf-8").splitlines() == [
        '1 ROLE "ANALYST"',
        '2 ROLE "LOADER"',
        '3 ROLE "AUDITOR"',
    ]


def test_snowflakes_own_names_are_left_as_they_are(pseudonymized: None) -> None:
    journal.user("role {} -> {}", journal.role("SYSADMIN"), journal.role("ETL"))
    journal.user(
        "cursor rested on {}",
        journal.subject(ObjectRef(ObjectType.VIEW, "SNOWFLAKE", "ACCOUNT", "PUBLIC")),
    )
    assert _pseudonymized() == [
        "user  role SYSADMIN -> ROLE_1",
        "user  cursor rested on view SNOWFLAKE.SCH_2.PUBLIC",
    ]


def test_an_error_keeps_its_wording_and_loses_its_names(pseudonymized: None) -> None:
    scope = pipe_history_scope("SALES", "RAW", "LOAD_ORDERS")
    journal.sent(scope, journal.Reason.F5)
    journal.failed(
        scope,
        "SQL compilation error: Object 'SALES.RAW.LOAD_ORDERS' does not exist, "
        "nor does 'SALES.RAW.SECRET_THING' or sales.raw",
    )
    journal.connection(
        "login failed: {}",
        journal.scrubbed(
            "250001: Could not connect to myorg-acct.snowflakecomputing.com:443 "
            "as jane.doe@example.com, see https://docs.example.com/x"
        ),
    )

    _, fail, conn = _pseudonymized()
    assert fail == (
        "fail  pipe_history:DB_1.SCH_2.PIPE_3  SQL compilation error: Object "
        "'DB_1.SCH_2.PIPE_3' does not exist, nor does 'DB_1.SCH_2.?' or DB_1.SCH_2"
    )
    assert conn == (
        "conn  login failed: 250001: Could not connect to <host> as <email>, see <host>"
    )


def test_the_file_shows_names_with_the_setting_off() -> None:
    config.set(JOURNAL_FILE, True)
    journal.user("databases added: {}", journal.joined(map(journal.database, "AB")))
    [file] = _files()
    assert file.read_text(encoding="utf-8").endswith("user  databases added: A, B\n")
    assert not _names_file().exists()


def test_no_name_reaches_the_file_when_the_placeholders_cannot_be_kept(
    pseudonymized: None, caplog: pytest.LogCaptureFixture
) -> None:
    _names_file().mkdir(parents=True)  # a directory where the file should be
    journal.user("expanded {}", journal.subject("SALES"))

    assert _lines(caplog) == ["user  expanded database SALES"]
    assert "SALES" not in "".join(p.read_text() for p in _files())
    assert any("cannot be written" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------- completeness


def _unreasoned_requests() -> list[str]:
    """Every call in src/ that asks for a scope without naming why.

    `request` is FetchService's, `_request` MainScreen's (reason third, or
    by name), `ScopeNeeded` the widgets' message.
    """
    import ast

    source = Path(journal.__file__).parent
    found = []
    for path in sorted(source.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name not in ("request", "_request", "ScopeNeeded"):
                continue
            named = any(keyword.arg == "reason" for keyword in node.keywords)
            positional = name == "_request" and len(node.args) >= 3
            if not (named or positional):
                found.append(f"{path.relative_to(source)}:{node.lineno} {name}")
    return found


def test_every_request_in_the_code_names_its_trigger() -> None:
    """Reason.UNKNOWN in a journal means a call site was missed."""
    assert _unreasoned_requests() == []


def _baked_in_names() -> list[str]:
    """Every journal call in src/ that hands over finished text.

    An f-string renders its names before the file's formatter can replace
    them. That covers a call on `journal.` and any `detail=`, which reaches
    the journal through `request`.
    """
    import ast

    source = Path(journal.__file__).parent
    found = []
    for path in sorted(source.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            on_journal = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "journal"
            )
            arguments = (
                [*node.args, *(k.value for k in node.keywords)] if on_journal else []
            )
            arguments += [k.value for k in node.keywords if k.arg == "detail"]
            if any(isinstance(argument, ast.JoinedStr) for argument in arguments):
                found.append(f"{path.relative_to(source)}:{node.lineno}")
    return found


def test_no_journal_call_in_the_code_bakes_a_name_into_its_text() -> None:
    assert _baked_in_names() == []
