# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Detail view for tasks: run history and SQL.

Deliberately a different shape from the column views: a task has no columns,
so its body is tabbed instead -- History (one row per execution, the same
dual-source pattern `pipe_view.py` uses, plus a detail panel below for
whatever the highlighted row wants to say about itself -- currently its
error, if any) and SQL (the task's raw definition, unformatted).

The history merges two sources: `task_activity_history_scope` (SNOWFLAKE.
ACCOUNT_USAGE.TASK_HISTORY, this one task's full backfill within the "task
history fetch window" setting -- lazy, fetched only while this view is open,
unlike the schema-wide `task_activity_scope` the "Tasks (n)" list uses, which
is reduced to each task's latest row only) and `task_history_scope`
(INFORMATION_SCHEMA.TASK_HISTORY, near-real-time but limited to Snowflake's
own 7-day retention for that function). Rows are merged by
(SCHEDULED_TIME, ATTEMPT_NUMBER), with the near-real-time source inserted
last so it wins on overlap. Not by QUERY_ID, the obvious candidate: a run
that has not started yet, or that failed before a query was submitted, has
none, and every such run would collapse into one row. Both sources carry the
same SCHEDULED_TIME for a run -- it is the planned instant, not a time each
view stamps for itself the way they do a load's LAST_LOAD_TIME (see
docs/snowflake-behavior.md "Task history"). The result is filtered to the
"task history display window" setting, which can be shorter than the fetch
window: shrinking it later is a display-only change, since the wider range
stays cached either way.

Unlike `pipe_view.py`, this view needs no target-table resolution and no
gating on `scopes()`: INFORMATION_SCHEMA.TASK_HISTORY accepts TASK_NAME/
DATABASE_NAME/SCHEMA_NAME as direct named arguments, all of which this
task's own `ref` already carries -- there is nothing here for a side channel
to resolve, unlike a pipe's target table, which Snowflake exposes nowhere
except by reading it back off cached copy-history data.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar

from rich.text import Text
from tcoda.widgets import TabbedContent, TabPane
from textual.app import ComposeResult
from textual.widgets import DataTable, TextArea

from nivaria.format import format_timestamp_minutes, parse_timestamp
from nivaria.models import (
    ObjectType,
    Scope,
    task_activity_history_scope,
    task_history_scope,
)
from nivaria.settings import TASK_HISTORY_SHOW_DAYS, config
from nivaria.snowflake.queries import TASK_HISTORY_RETENTION_DAYS
from nivaria.ui.views.base import ObjectView, register_view
from nivaria.ui.views.tables import (
    HISTORY_TABLE,
    SQL,
    HistoryView,
)

_HISTORY_COLUMNS = ("Scheduled", "Started", "Completed", "State")


def _merge_key(row: dict[str, Any]) -> tuple[Any, Any]:
    """One run's identity across both history sources -- see the module
    docstring. The scheduled time is compared as an instant, falling back to
    its text when it does not parse."""
    scheduled = row.get("SCHEDULED_TIME")
    return (parse_timestamp(scheduled) or scheduled, row.get("ATTEMPT_NUMBER"))


@register_view
class TaskView(HistoryView, ObjectView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.TASK

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-history"):
            with TabPane("History", mnemonic="h", id="tab-history"):
                yield from self.compose_history()
            with TabPane("SQL", mnemonic="s", id="tab-sql"):
                yield TextArea(
                    "",
                    id=SQL,
                    read_only=True,
                    soft_wrap=True,
                    show_line_numbers=False,
                )

    def activity_history_scope(self) -> Scope:
        return task_activity_history_scope(
            self.ref.database, self.ref.schema, self.ref.name
        )

    def history_scope(self) -> Scope:
        return task_history_scope(self.ref.database, self.ref.schema, self.ref.name)

    def scopes(self) -> list[Scope]:
        # No gating here, unlike PipeView -- see the module docstring: both
        # sources take this task's identity directly, with nothing to resolve
        # first, so both are always meaningfully "on screen". The listing is
        # here so the SQL tab repaints when it lands.
        return [
            self.listing_scope(),
            self.activity_history_scope(),
            self.history_scope(),
        ]

    def update_content(self) -> None:
        self._update_history()
        self._update_definition()

    def _update_history(self) -> None:
        activity_history_scope = self.activity_history_scope()
        history_scope = self.history_scope()

        # Merged by (SCHEDULED_TIME, ATTEMPT_NUMBER) -- see the module
        # docstring for why that key and not QUERY_ID.
        merged: dict[tuple[Any, Any], dict[str, Any]] = {}
        for r in self.repo.raw_rows(activity_history_scope):
            merged[_merge_key(r)] = r
        for r in self.repo.raw_rows(history_scope):
            merged[_merge_key(r)] = r

        show_days = config.get(TASK_HISTORY_SHOW_DAYS)
        cutoff = datetime.now().astimezone() - timedelta(days=show_days)
        dated: list[tuple[datetime, dict[str, Any]]] = []
        for r in merged.values():
            scheduled_at = parse_timestamp(r.get("SCHEDULED_TIME"))
            if scheduled_at is not None and scheduled_at >= cutoff:
                dated.append((scheduled_at, r))
        dated.sort(key=lambda pair: pair[0], reverse=True)

        # Both merge sources carry their own row cap, and either one hitting
        # it leaves the merged table short -- see HistoryView. Judged against
        # `dated`, not the merge sources' own row counts -- see
        # HistoryView.sync_history_note.
        self.sync_history_note(
            self.repo,
            dated,
            show_days,
            "run history",
            [
                (activity_history_scope, None),
                (history_scope, TASK_HISTORY_RETENTION_DAYS),
            ],
        )

        table = self.query_one(f"#{HISTORY_TABLE}", DataTable)
        with self.keeping_cursor(table):
            table.clear(columns=True)
            table.add_columns(*_HISTORY_COLUMNS)
            rows = self.history_panel.rows = {}
            for _, r in dated:
                # The run's own identity, not its position, so the cursor is
                # found again by key after a repaint -- same as PipeView.
                key = str(_merge_key(r))
                rows[key] = r
                table.add_row(
                    Text(format_timestamp_minutes(r.get("SCHEDULED_TIME"))),
                    Text(format_timestamp_minutes(r.get("QUERY_START_TIME"))),
                    Text(format_timestamp_minutes(r.get("COMPLETED_TIME"))),
                    Text(r.get("STATE") or ""),
                    key=key,
                )
        self.history_panel.sync()

    def _update_definition(self) -> None:
        row = self.repo.object_detail(self.ref)
        text = (row or {}).get("definition") or "-- not cached"
        self.query_one(f"#{SQL}", TextArea).text = text

    def detail_text(self, row: dict[str, Any]) -> str | None:
        """A failed run's own error, for the panel under the history table."""
        message = row.get("ERROR_MESSAGE")
        if not message:
            return None
        code = row.get("ERROR_CODE")
        return f"[{code}] {message}" if code is not None else message
