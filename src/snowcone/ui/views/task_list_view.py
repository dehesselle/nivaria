"""Category view for the "Tasks (n)" node: an overview of every task in a
schema, merging schedule/state with last-run activity into one table.

Deliberately not tabbed like `pipe_list_view.py`: there is only this one
thing to show for a schema's tasks right now, so a single DataTable is
enough -- the same shape `table_list_view.py` already uses for the same
reason.

The base columns are sourced entirely from `SHOW TASKS`, already mirrored for
the tree's own category count -- no new fetch scope for those. "Last
Scheduled" and "Last Run State" are this app's columns sourced from an
ordinary `SELECT`
against SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY instead of `SHOW` (see
DESIGN.md's dual-source note): `SHOW TASKS` carries no run-history data at
all. That SELECT needs a running warehouse and elevated privileges, unlike
everything else in this view, so it lives in its own scope
(`task_activity_scope`) with its own freshness -- a session missing either
just leaves those two columns blank (see `Repository.fail_pass`) while the
SHOW-sourced columns keep rendering whatever was cached before.

Three columns here are Snowflake's own words, kept rather than smoothed over
-- DESIGN.md's "mirror Snowflake, don't reinterpret it" rule:

* "State" is `show_tasks.state` (started/suspended): whether the task is
  enabled at all.
* "Last Run State" is `TASK_HISTORY.STATE` (SUCCEEDED/FAILED/SKIPPED/...):
  how the most recent run ended. Snowflake genuinely names both columns
  `state`; the "Last Run" prefix is the minimum needed to tell two columns in
  one table apart, not a rename.
* "Last Scheduled" is `TASK_HISTORY.SCHEDULED_TIME`, not a run's start time.
  A task whose run was scheduled but never started still has one, so calling
  it "Last Run" claimed something the column does not say. A run's actual
  start and finish are in `TaskView`'s own Run history tab.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable, Static

from snowcone.cache.repository import Repository
from snowcone.format import format_timestamp_minutes
from snowcone.models import ObjectRef, ObjectType, Scope, task_activity_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.views.base import CategoryView, register_category_view
from snowcone.ui.views.tables import (
    LIST_NOTE,
    LIST_TABLE,
    HoverTooltipTable,
    sync_list_note,
)

_COLUMNS = (
    "Name",
    "Warehouse",
    "Schedule",
    "State",
    "Last Scheduled",
    "Last Run State",
    "Comment",
)

#: Which merged-row field a tooltip-bearing column's full value comes
#: from, keyed by that column's header. Every other column has no tooltip.
_TOOLTIP_FIELDS = {"Last Scheduled": "SCHEDULED_TIME"}


@register_category_view
class TaskListView(CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.TASK

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by task name -- the merged show_tasks +
        #: activity dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id=LIST_NOTE)
        table: DataTable[Text] = HoverTooltipTable(
            self, _TOOLTIP_FIELDS, id=LIST_TABLE, cursor_type="row"
        )
        table.zebra_stripes = True
        yield table

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that task's detail view, revealed in the
        tree -- see PipeListView's identical handler for the full reasoning."""
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectType.TASK, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectSelected(ref))

    def activity_scope(self) -> Scope:
        return task_activity_scope(self.scope.database, self.scope.schema)

    def scopes(self) -> list[Scope]:
        return [self.scope, self.activity_scope()]

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        self.ensure(self.scope, ttl)
        sync_list_note(
            self.query_one(f"#{LIST_NOTE}", Static),
            self.repo.entry(self.scope),
            "tasks",
        )
        activity_scope = self.activity_scope()
        self.ensure(activity_scope, ttl)
        # The cache already holds at most one row per task -- queries.
        # task_activity's QUALIFY keeps only each task's latest run. Keyed by
        # NAME so a missing/failed activity pass just means an empty dict
        # here -- every row still renders from show_tasks alone.
        activity_by_name: dict[str, dict[str, Any]] = {
            name: row
            for row in self.repo.raw_rows(activity_scope)
            if (name := row.get("NAME"))
        }

        table = self.query_one(f"#{LIST_TABLE}", DataTable)
        with self.keeping_cursor(table):
            table.clear(columns=True)
            table.add_columns(*_COLUMNS)
            self._rows = {}
            for row in self.repo.raw_rows(self.scope):
                name = row["name"]
                merged = {**row, **activity_by_name.get(name, {})}
                self._rows[name] = merged
                table.add_row(
                    Text(name),
                    Text(row.get("warehouse") or ""),
                    Text(row.get("schedule") or ""),
                    Text(row.get("state") or ""),
                    Text(format_timestamp_minutes(merged.get("SCHEDULED_TIME"))),
                    Text(merged.get("STATE") or ""),
                    Text(row.get("comment") or ""),
                    key=name,
                )
