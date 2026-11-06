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

Status lives in `pipe_list_view.py` instead of
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
`pipe_activity_scope` or this pipe's own backfill has cached, or else off a
fully qualified target in the pipe's definition. A pipe with none of them has
no target to give it. That is
a normal, lasting state, not a transient failure: this view does not list the
history scope in `scopes()` until a target is actually resolvable, so such a
pipe does not repeatedly fail a fetch it can never succeed at. The snapshot is then its only source, refreshed on the
cache age like everything else, and its first load gives it a target. Until
then, only ACCOUNT_USAGE speaks for it, so the History tab carries the lag
banner (`tables.lag_note`).

`pipe_activity_history_scope` is gated too, on the snapshot not having
answered without a load for this pipe (`needs_backfill`). For a pipe with no
load in the snapshot, the snapshot itself is the history's older half: it
covers the fetch window up to its last refresh, at most one cache age ago,
and the 14-day history the time since, so the backfill could only repeat its
"nothing" (docs/DESIGN.md "The pipe the snapshot shows no load for"). Until
the snapshot answers, the backfill is asked for alongside it: it needs no
target table, being filtered by PIPE_NAME, and it answers sooner.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar

from rich.text import Text
from tcoda.widgets import TabbedContent, TabPane
from textual.app import ComposeResult
from textual.widgets import DataTable, Static, TextArea

from nivaria.format import format_bytes, format_timestamp_minutes, parse_timestamp
from nivaria.models import (
    CacheState,
    ObjectType,
    Scope,
    pipe_activity_history_scope,
    pipe_activity_scope,
    pipe_history_scope,
)
from nivaria.settings import PIPE_HISTORY_SHOW_DAYS, config
from nivaria.snowflake.queries import (
    COPY_HISTORY_PUBLISHED_LAG,
    PIPE_HISTORY_RETENTION_DAYS,
)
from nivaria.ui.views.base import ObjectView, register_view
from nivaria.ui.views.tables import (
    HISTORY_TABLE,
    LAG_NOTE,
    SQL,
    HistoryView,
    lag_note,
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

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-history"):
            with TabPane("History", mnemonic="h", id="tab-history"):
                yield lag_note("Load activity", COPY_HISTORY_PUBLISHED_LAG)
                yield from self.compose_history()
            with TabPane("SQL", mnemonic="s", id="tab-sql"):
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

        Not once the PIPE_ACTIVITY snapshot has answered without a load for
        this pipe. The backfill reads the same view over the same window
        with the same lag, and can only repeat the snapshot's "nothing": the
        snapshot covers the window up to its last refresh, the 14-day
        history from there to now (see the module docstring).

        Until the snapshot has answered, it is asked for alongside it rather
        than after it. The schema-wide snapshot can take half a minute on a
        busy account, one pipe's backfill a few seconds, and the backfill
        also names the target table the 14-day history needs
        (`Repository.pipe_target_table`). Waiting would leave the view empty
        that long; not waiting costs one backfill for a pipe that turns out
        idle, once, since later refreshes are window passes. Not while the
        snapshot's only attempt has failed, though: the backfill reads the
        same view and would fail the same way.
        """
        entry = self.repo.entry(self.activity_scope())
        if entry.first_fetch_failed:
            return False
        if entry.fetched_at is None:
            return True
        idle = self.repo.pipe_idle_as_of(
            self.ref.database, self.ref.schema, self.ref.name
        )
        return idle is None

    def scopes(self) -> list[Scope]:
        # history_scope is only meaningfully "on screen" once a target table
        # is resolvable -- see the module docstring. Omitting it otherwise
        # keeps both `ensure_all` and the background sweep (MainScreen._sweep,
        # which watches self.detail.scopes()) from ever requesting a fetch
        # that cannot succeed. activity_history_scope is
        # gated the same way, on needs_backfill. The listing is here so the
        # SQL tab repaints when it lands.
        scopes = [self.listing_scope(), self.activity_scope()]
        if self.needs_backfill():
            scopes.append(self.activity_history_scope())
        if self.target_table() is not None:
            scopes.append(self.history_scope())
        return scopes

    def update_content(self) -> None:
        self._update_history()
        self._update_definition()

    def _update_history(self) -> None:
        # Not read into the merge -- it only ever holds this pipe's latest
        # row, which activity_history_scope's full backfill already covers --
        # but it resolves the target, decides whether the backfill is needed
        # at all, and is the answer for the fetch window when it is not.
        activity_scope = self.activity_scope()
        activity_history_scope = (
            self.activity_history_scope() if self.needs_backfill() else None
        )
        target = self.target_table()
        history_scope = self.history_scope() if target is not None else None

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
        self.sync_history_note(self.repo, dated, show_days, "load activity", sources)
        # Only ACCOUNT_USAGE speaks for a pipe without a target, or one whose
        # target cannot be read. Otherwise INFORMATION_SCHEMA covers the
        # recent days near real time.
        unread = (
            history_scope is None
            or self.repo.entry(history_scope).state is CacheState.ERROR
        )
        self.query_one(f"#{LAG_NOTE}", Static).display = unread

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
