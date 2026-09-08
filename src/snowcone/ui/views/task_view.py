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
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static, TabbedContent, TabPane, TextArea
from textual.widgets.data_table import CellDoesNotExist

from snowcone.cache.repository import Repository
from snowcone.format import format_timestamp_minutes, parse_timestamp
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    Scope,
    task_activity_history_scope,
    task_history_scope,
)
from snowcone.settings import CACHE_TTL, TASK_HISTORY_SHOW_DAYS, config
from snowcone.ui.views.base import ObjectView, register_view

_HISTORY_COLUMNS = ("Scheduled", "Started", "Completed", "Status")


@register_view
class TaskView(ObjectView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.TASK

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "show_tab('history')", "Run history", show=False),
        Binding("d", "show_tab('definition')", "Definition", show=False),
    ]

    def __init__(self, repo: Repository, ref: ObjectRef) -> None:
        super().__init__(repo, ref)
        #: This paint's history rows, keyed by QUERY_ID -- looked back up
        #: when a row is highlighted.
        self._history_rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-history"):
            with TabPane("[$accent bold]R[/]un history", id="tab-history"):
                history: DataTable[Text] = DataTable(
                    id="task-history", cursor_type="row"
                )
                history.zebra_stripes = True
                yield history
                yield Static("", id="task-history-detail")
            with TabPane("[$accent bold]D[/]efinition", id="tab-definition"):
                yield TextArea(
                    "",
                    id="task-definition",
                    read_only=True,
                    soft_wrap=True,
                    show_line_numbers=False,
                )

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = f"tab-{tab}"

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

        table = self.query_one("#task-history", DataTable)
        table.clear(columns=True)
        table.add_columns(*_HISTORY_COLUMNS)
        self._history_rows = {}
        for _, r in dated:
            key = str(r.get("QUERY_ID"))
            self._history_rows[key] = r
            table.add_row(
                Text(format_timestamp_minutes(r.get("SCHEDULED_TIME"))),
                Text(format_timestamp_minutes(r.get("QUERY_START_TIME"))),
                Text(format_timestamp_minutes(r.get("COMPLETED_TIME"))),
                Text(r.get("STATE") or ""),
                key=key,
            )
        self._sync_history_detail(table.cursor_row)

    def _update_definition(self) -> None:
        row = self.repo.object_detail(self.ref)
        text = (row or {}).get("definition") or "-- not cached"
        self.query_one("#task-definition", TextArea).text = text

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._sync_history_detail_from_key(
            event.row_key.value if event.row_key is not None else None
        )

    def _sync_history_detail(self, cursor_row: int) -> None:
        """Resync against whatever the cursor is actually on after a repaint
        -- not left to RowHighlighted alone, since it's not guaranteed to
        re-fire on a table.clear()-and-repopulate while a row is already
        highlighted."""
        table = self.query_one("#task-history", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(Coordinate(cursor_row, 0))
        except CellDoesNotExist:
            self._show_history_detail(None)
            return
        self._sync_history_detail_from_key(row_key.value)

    def _sync_history_detail_from_key(self, key: str | None) -> None:
        row = self._history_rows.get(key) if key is not None else None
        message = row.get("ERROR_MESSAGE") if row else None
        if not message:
            self._show_history_detail(None)
            return
        code = row.get("ERROR_CODE")
        text = f"[{code}] {message}" if code is not None else message
        self._show_history_detail(text)

    def _show_history_detail(self, text: str | None) -> None:
        panel = self.query_one("#task-history-detail", Static)
        if text:
            panel.update(Text(text))
            panel.display = True
        else:
            panel.display = False
