# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Detail view for pipes: load history and SQL.

Deliberately a different shape from the column views: a pipe has no columns,
so its body is tabbed instead -- History (one row per file it has
loaded, the same dual-source pattern `pipe_list_view.py` uses, plus a detail
panel below for whatever the highlighted row wants to say about itself --
currently its error, if any) and SQL (the pipe's raw COPY INTO definition,
unformatted).

Status (pipe/status/invalid_reason) lives in `pipe_list_view.py` instead of
here -- a single pipe's view can only ever show one row of it, which can
never answer the question a status tab is for ("which of my pipes are
broken?"). The category view already lists every pipe in the schema, so it
can show status for all of them at once.

The history merges two sources: `pipe_activity_history_scope` (SNOWFLAKE.
ACCOUNT_USAGE.COPY_HISTORY, this one pipe's full backfill within the "pipe
history fetch window" setting -- lazy, fetched only while this view is open,
unlike the schema-wide `pipe_activity_scope` the "Pipes (n)" list uses, which
is reduced to each pipe's latest row only) and `pipe_history_scope`
(INFORMATION_SCHEMA.COPY_HISTORY, near-real-time but limited to Snowflake's
own 14-day retention for that function). Rows are merged by (FILE_NAME,
PIPE_RECEIVED_TIME), with the near-real-time source inserted last so it wins
on overlap -- robust to either source being empty or failed, since there is no
need to reason about which scope succeeded. Not by LAST_LOAD_TIME, the
obvious candidate: the two views stamp that independently, tens of
milliseconds apart for the same load on a live account, so keying on it
showed every overlapping load twice. The result is filtered to the
"pipe history display window" setting, which can be shorter than the fetch
window: shrinking it later is a display-only change, since the wider range
stays cached either way.

`pipe_history_scope` needs the pipe's target table, which Snowflake exposes
in no named field: `Repository.pipe_target_table` reads it off whatever
`pipe_activity_scope` has cached, or else off a fully qualified target in
the pipe's definition. A pipe with neither has no target to give it. That is
a normal, lasting state, not a transient failure: this view does not
`ensure()` -- or even list in `scopes()` -- the history scope until a target
is actually resolvable, so such a pipe does not repeatedly fail a fetch it
can never succeed at. Nothing on the cache-age cadence watches it then, and
its note says so (`_unchecked_note`).

`pipe_activity_history_scope` is gated too, on the snapshot showing a load
for this pipe (`needs_backfill`). For a pipe with no load in the snapshot,
the snapshot itself is the history's older half: it covers the fetch window
up to its fetch, at most 13 days ago (its TTL), and the 14-day history the
time since, so the backfill could only repeat its "nothing" (docs/DESIGN.md
"The pipe the snapshot shows no load for"). `pipe_activity_history_scope`
needs no such gating: ACCOUNT_USAGE.COPY_HISTORY filters directly by
PIPE_NAME, with no target table to resolve first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, TabbedContent, TabPane, TextArea

from nivaria.format import format_bytes, format_timestamp_minutes, parse_timestamp
from nivaria.gateway.queries import PIPE_HISTORY_RETENTION_DAYS
from nivaria.models import (
    CacheState,
    ObjectType,
    Scope,
    pipe_activity_history_scope,
    pipe_activity_scope,
    pipe_history_scope,
)
from nivaria.settings import (
    CACHE_TTL,
    PIPE_HISTORY_FETCH_DAYS,
    PIPE_HISTORY_SHOW_DAYS,
    config,
)
from nivaria.ui.views.base import ObjectView, register_view
from nivaria.ui.views.tables import (
    HISTORY_TABLE,
    SQL,
    HistoryView,
)

_HISTORY_COLUMNS = ("Rows", "Size", "Loaded", "Status", "File Name")


def _merge_key(row: dict[str, Any]) -> tuple[Any, Any]:
    """One load's identity across both history sources -- see the module
    docstring. Falls back to LAST_LOAD_TIME for a row without a
    PIPE_RECEIVED_TIME, so several such loads of one file name are not
    collapsed into one."""
    received = row.get("PIPE_RECEIVED_TIME")
    return (row.get("FILE_NAME"), received or row.get("LAST_LOAD_TIME"))


@register_view
class PipeView(HistoryView, ObjectView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.PIPE

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("h", "show_tab('history')", "History", show=False),
        *HistoryView.BINDINGS,
    ]

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-history"):
            with TabPane("[$accent bold]H[/]istory", id="tab-history"):
                yield from self.compose_history()
            with TabPane("[$accent bold]S[/]QL", id="tab-sql"):
                yield TextArea(
                    "",
                    id=SQL,
                    read_only=True,
                    soft_wrap=True,
                    show_line_numbers=False,
                )

    def activity_scope(self) -> Scope:
        return pipe_activity_scope(self.ref.database, self.ref.schema)

    def activity_history_scope(self) -> Scope:
        return pipe_activity_history_scope(
            self.ref.database, self.ref.schema, self.ref.name
        )

    def history_scope(self) -> Scope:
        return pipe_history_scope(self.ref.database, self.ref.schema, self.ref.name)

    def target_table(self) -> tuple[str, str, str] | None:
        return self.repo.pipe_target_table(
            self.ref.database, self.ref.schema, self.ref.name
        )

    def needs_backfill(self) -> bool:
        """Whether this pipe's own ACCOUNT_USAGE backfill can add anything.

        Only once the PIPE_ACTIVITY snapshot has answered with a load for
        this pipe. Without one, the backfill reads the same view over the
        same window with the same lag, and can only repeat the snapshot's
        "nothing": the snapshot covers the window up to its fetch, the 14-day
        history from there to now (see the module docstring). Asking before
        the snapshot answers would pay the full 90-day statement for a pipe
        that may turn out to have nothing to show.
        """
        entry = self.repo.entry(self.activity_scope())
        if entry.fetched_at is None or entry.state is CacheState.EMPTY:
            return False
        idle = self.repo.pipe_idle_as_of(
            self.ref.database, self.ref.schema, self.ref.name
        )
        return idle is None

    def scopes(self) -> list[Scope]:
        # history_scope is only meaningfully "on screen" once a target table
        # is resolvable -- see the module docstring. Omitting it otherwise
        # keeps both the interactive ensure() below and the background sweep
        # (MainScreen._sweep, which watches self.detail.scopes()) from ever
        # requesting a fetch that cannot succeed. activity_history_scope is
        # gated the same way, on needs_backfill.
        scopes = [self.activity_scope()]
        if self.needs_backfill():
            scopes.append(self.activity_history_scope())
        if self.target_table() is not None:
            scopes.append(self.history_scope())
        return scopes

    def update_content(self) -> None:
        self._update_history()
        self._update_definition()

    def _update_history(self) -> None:
        ttl = config.get(CACHE_TTL)
        # Not read into the merge -- it only ever holds this pipe's latest
        # row, which activity_history_scope's full backfill already covers --
        # but it resolves the target, decides whether the backfill is needed
        # at all, and is the answer for the fetch window when it is not.
        activity_scope = self.activity_scope()
        self.ensure(activity_scope, ttl)
        activity_history_scope = (
            self.activity_history_scope() if self.needs_backfill() else None
        )
        if activity_history_scope is not None:
            self.ensure(activity_history_scope, ttl)

        target = self.target_table()
        history_scope = self.history_scope() if target is not None else None
        if history_scope is not None:
            self.ensure(history_scope, ttl)

        # Merged by (FILE_NAME, PIPE_RECEIVED_TIME) -- see the module
        # docstring for why that key, and why this needs no "which scope
        # succeeded" branching.
        merged: dict[tuple[Any, Any], dict[str, Any]] = {}
        if activity_history_scope is not None:
            for r in self.repo.raw_rows(activity_history_scope):
                merged[_merge_key(r)] = r
        if history_scope is not None:
            for r in self.repo.raw_rows(history_scope):
                merged[_merge_key(r)] = r

        show_days = config.get(PIPE_HISTORY_SHOW_DAYS)
        cutoff = datetime.now().astimezone() - timedelta(days=show_days)
        dated: list[tuple[datetime, dict[str, Any]]] = []
        for r in merged.values():
            loaded_at = parse_timestamp(r.get("LAST_LOAD_TIME"))
            if loaded_at is not None and loaded_at >= cutoff:
                dated.append((loaded_at, r))
        dated.sort(key=lambda pair: pair[0], reverse=True)

        # Both merge sources carry their own row cap, and either one hitting
        # it leaves the merged table short -- see HistoryView. Judged against
        # `dated`, not the merge sources' own row counts -- see
        # HistoryView.sync_history_note.
        # The snapshot is a source in its own right: without the backfill it
        # is the answer for the window before the 14-day history, and the
        # backfill is not asked for until it has answered. Like the backfill,
        # it lags, so it claims nothing early.
        backfill = activity_history_scope or activity_scope
        sources: list[tuple[Scope, int | None]] = [(backfill, None)]
        if history_scope is not None:
            sources.append((history_scope, PIPE_HISTORY_RETENTION_DAYS))
        unchecked = None
        if history_scope is None:
            days = min(show_days, config.get(PIPE_HISTORY_FETCH_DAYS))
            unchecked = self._unchecked_note(bool(dated), days)
        self.sync_history_note(
            self.repo,
            dated,
            show_days,
            "load activity",
            sources,
            unchecked=unchecked,
        )

        table = self.query_one(f"#{HISTORY_TABLE}", DataTable)
        with self.keeping_cursor(table):
            table.clear(columns=True)
            table.add_columns(*_HISTORY_COLUMNS)
            rows = self.history_panel.rows = {}
            for _, r in dated:
                # The load's own identity, not its position: a new load arriving
                # at the top would otherwise shift every row's key, and the
                # cursor is found again by key after a repaint.
                key = str(_merge_key(r))
                rows[key] = r
                row_count = r.get("ROW_COUNT")
                table.add_row(
                    Text(f"{row_count:,}" if row_count is not None else ""),
                    Text(format_bytes(r.get("FILE_SIZE"))),
                    Text(format_timestamp_minutes(r.get("LAST_LOAD_TIME"))),
                    Text(r.get("STATUS") or ""),
                    Text(r.get("FILE_NAME") or ""),
                    key=key,
                )
        self.history_panel.sync()

    def _unchecked_note(self, has_rows: bool, days: int) -> str | None:
        """The note for a pipe without a target table, once anything answered.

        Without a target only ACCOUNT_USAGE speaks for this pipe, and both of
        its scopes carry the 13-day TTL override, so nothing on the cache-age
        cadence notices a new load. That gap has to show, with the time of
        the newest answer and the way to check now.
        """
        read = [self.activity_scope()]
        if self.needs_backfill():
            read.append(self.activity_history_scope())
        fetched = [
            entry.fetched_at
            for entry in map(self.repo.entry, read)
            if entry.fetched_at is not None and entry.state is not CacheState.EMPTY
        ]
        if not fetched:
            return None
        # A time, not an age -- see tables.failed_refresh.
        as_of = datetime.fromtimestamp(max(fetched), UTC).astimezone()
        known = (
            f"Shown as of {as_of:%Y-%m-%d %H:%M}."
            if has_rows
            else f"No load activity in the last {days} days as of "
            f"{as_of:%Y-%m-%d %H:%M}."
        )
        return (
            f"{known} This pipe's target table can't be read from its "
            "definition, so new loads are not checked for automatically. "
            "Press F5 to check now."
        )

    def _update_definition(self) -> None:
        row = self.repo.object_detail(self.ref)
        text = (row or {}).get("definition") or "-- not cached"
        self.query_one(f"#{SQL}", TextArea).text = text

    def detail_text(self, row: dict[str, Any]) -> str | None:
        """A failed load's own error, for the panel under the history table."""
        message = row.get("FIRST_ERROR_MESSAGE")
        if not message:
            return None
        line = row.get("FIRST_ERROR_LINE_NUMBER")
        return f"Line {line}: {message}" if line is not None else message
