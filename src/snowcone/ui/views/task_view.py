"""Detail view for tasks: run history and definition.

Deliberately a different shape from the column views: a task has no columns,
so its body is tabbed instead -- Run history (one row per execution, the same
dual-source pattern `pipe_view.py` uses, plus a detail panel below for
whatever the highlighted row wants to say about itself -- currently its
error, if any) and Definition (the task's raw SQL statement, unformatted).

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
from textual.widgets import DataTable, Static, TabbedContent, TabPane, TextArea

from snowcone.cache.repository import Repository
from snowcone.format import format_timestamp_minutes, parse_timestamp
from snowcone.models import (
    ObjectRef,
    ObjectType,
    Scope,
    task_activity_history_scope,
    task_history_scope,
)
from snowcone.settings import CACHE_TTL, TASK_HISTORY_SHOW_DAYS, config
from snowcone.ui.views.base import ObjectView, register_view
from snowcone.ui.views.tables import (
    DEFINITION,
    HISTORY_NOTE,
    HISTORY_TABLE,
    ITEM_DETAIL,
    HistoryView,
)

_HISTORY_COLUMNS = ("Scheduled", "Started", "Completed", "State")


@register_view
class TaskView(HistoryView, ObjectView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.TASK

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "show_tab('history')", "Run history", show=False),
        *HistoryView.BINDINGS,
    ]

    def __init__(self, repo: Repository, ref: ObjectRef) -> None:
        super().__init__(repo, ref)
        #: Keyed by QUERY_ID here, unlike PipeView's positional key: a task
        #: run has Snowflake's own identifier, a pipe's file load does not.
        self.detail_rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-history"):
            with TabPane("[$accent bold]R[/]un history", id="tab-history"):
                yield Static("", id=HISTORY_NOTE)
                history: DataTable[Text] = DataTable(
                    id=HISTORY_TABLE, cursor_type="row"
                )
                history.zebra_stripes = True
                yield history
                yield Static("", id=ITEM_DETAIL, classes="-error")
            with TabPane("[$accent bold]D[/]efinition", id="tab-definition"):
                yield TextArea(
                    "",
                    id=DEFINITION,
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
        table.clear(columns=True)
        table.add_columns(*_HISTORY_COLUMNS)
        self.detail_rows = {}
        for _, r in dated:
            key = str(r.get("QUERY_ID"))
            self.detail_rows[key] = r
            table.add_row(
                Text(format_timestamp_minutes(r.get("SCHEDULED_TIME"))),
                Text(format_timestamp_minutes(r.get("QUERY_START_TIME"))),
                Text(format_timestamp_minutes(r.get("COMPLETED_TIME"))),
                Text(r.get("STATE") or ""),
                key=key,
            )
        self.sync_item_detail(table.cursor_row)

    def _update_definition(self) -> None:
        row = self.repo.object_detail(self.ref)
        text = (row or {}).get("definition") or "-- not cached"
        self.query_one(f"#{DEFINITION}", TextArea).text = text

    def detail_text(self, row: dict[str, Any]) -> str | None:
        """A failed run's own error, for the panel under the history table."""
        message = row.get("ERROR_MESSAGE")
        if not message:
            return None
        code = row.get("ERROR_CODE")
        return f"[{code}] {message}" if code is not None else message
