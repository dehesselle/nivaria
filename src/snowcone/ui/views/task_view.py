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
own 7-day retention for that function). Rows are merged by QUERY_ID alone,
with the near-real-time source inserted last so it wins on overlap --
Snowflake's own unique identifier for one run, present in both sources, so
unlike `pipe_view.py` (which has no such id for a COPY_HISTORY load and needs
a compound `(FILE_NAME, LAST_LOAD_TIME)` key) a single-column key is both
correct and simpler here. The result is filtered to the "task history display
window" setting, which can be shorter than the fetch window: shrinking it
later is a display-only change, since the wider range stays cached either
way.

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
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, TabbedContent, TabPane, TextArea

from snowcone.format import format_timestamp_minutes, parse_timestamp
from snowcone.models import (
    ObjectType,
    Scope,
    task_activity_history_scope,
    task_history_scope,
)
from snowcone.settings import CACHE_TTL, TASK_HISTORY_SHOW_DAYS, config
from snowcone.ui.views.base import ObjectView, register_view
from snowcone.ui.views.tables import (
    HISTORY_TABLE,
    SQL,
    HistoryView,
)

_HISTORY_COLUMNS = ("Scheduled", "Started", "Completed", "State")


@register_view
class TaskView(HistoryView, ObjectView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.TASK

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

    def activity_history_scope(self) -> Scope:
        return task_activity_history_scope(
            self.ref.database, self.ref.schema, self.ref.name
        )

    def history_scope(self) -> Scope:
        return task_history_scope(self.ref.database, self.ref.schema, self.ref.name)

    def scopes(self) -> list[Scope]:
        # No gating here, unlike PipeView -- see the module docstring: both
        # sources take this task's identity directly, with nothing to resolve
        # first, so both are always meaningfully "on screen".
        return [self.activity_history_scope(), self.history_scope()]

    def update_content(self) -> None:
        self._update_history()
        self._update_definition()

    def _update_history(self) -> None:
        ttl = config.get(CACHE_TTL)
        activity_history_scope = self.activity_history_scope()
        self.ensure(activity_history_scope, ttl)
        history_scope = self.history_scope()
        self.ensure(history_scope, ttl)

        # Merged by QUERY_ID -- see the module docstring for why this needs
        # no target resolution or compound key the way pipe's does.
        merged: dict[Any, dict[str, Any]] = {}
        for r in self.repo.raw_rows(activity_history_scope):
            merged[r.get("QUERY_ID")] = r
        for r in self.repo.raw_rows(history_scope):
            merged[r.get("QUERY_ID")] = r

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
            activity_history_scope,
            history_scope,
        )

        table = self.query_one(f"#{HISTORY_TABLE}", DataTable)
        with self.keeping_cursor(table):
            table.clear(columns=True)
            table.add_columns(*_HISTORY_COLUMNS)
            rows = self.history_panel.rows = {}
            for _, r in dated:
                # Keyed by QUERY_ID, unlike PipeView's merge key: a task run
                # has Snowflake's own identifier, a pipe's file load does not.
                key = str(r.get("QUERY_ID"))
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
