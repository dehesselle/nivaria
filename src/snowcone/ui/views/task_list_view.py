"""Category view for the "Tasks (n)" node: an overview of every task in a
schema, merging schedule/state with last-run activity into one table.

Deliberately not tabbed like `pipe_list_view.py`: there is only this one
thing to show for a schema's tasks right now, so a single DataTable is
enough -- the same shape `table_list_view.py` already uses for the same
reason.

The base columns are sourced entirely from `SHOW TASKS`, already mirrored for
the tree's own category count -- no new fetch scope for those. "Last Run" and
"Last Run Status" are this app's columns sourced from an ordinary `SELECT`
against SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY instead of `SHOW` (see
DESIGN.md's dual-source note): `SHOW TASKS` carries no run-history data at
all. That SELECT needs a running warehouse and elevated privileges, unlike
everything else in this view, so it lives in its own scope
(`task_activity_scope`) with its own freshness -- a session missing either
just leaves those two columns blank (see `Repository.fail_pass`) while the
SHOW-sourced columns keep rendering whatever was cached before.

"State" (started/suspended, `show_tasks.state`) is shown verbatim, not
relabelled -- DESIGN.md's "mirror Snowflake, don't reinterpret it" rule. It
is Snowflake's own vocabulary for whether the task is enabled, unrelated to
"Last Run Status" (SUCCEEDED/FAILED/...), which the adjacent column name
already disambiguates.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist

from snowcone.cache.repository import Repository
from snowcone.format import format_timestamp_full, format_timestamp_minutes
from snowcone.models import ObjectKind, ObjectRef, Scope, task_activity_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.views.base import CategoryView, register_category_view

_COLUMNS = (
    "Name",
    "Warehouse",
    "Schedule",
    "State",
    "Last Run",
    "Last Run Status",
    "Comment",
)

#: Which merged-row field a tooltip-bearing column's full value comes from,
#: by column index. Every other column has no tooltip.
_TOOLTIP_FIELDS = {4: "SCHEDULED_TIME"}


class _TaskListDataTable(DataTable):
    """Tooltips the full-precision "Last Run" timestamp on hover.

    Same pattern as `pipe_list_view._PipeListDataTable`/`table_list_view.
    _TableListDataTable` -- duplicated rather than shared, matching their own
    precedent.
    """

    def __init__(self, view: TaskListView, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._view = view

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        super()._on_mouse_move(event)
        meta = event.style.meta
        if not meta or meta.get("out_of_bounds", False):
            self.tooltip = None

    def watch_hover_coordinate(self, old: Coordinate, value: Coordinate) -> None:
        super().watch_hover_coordinate(old, value)
        field = _TOOLTIP_FIELDS.get(value.column)
        if field is None:
            self.tooltip = None
            return
        try:
            row_key, _ = self.coordinate_to_cell_key(value)
        except CellDoesNotExist:
            self.tooltip = None
            return
        row = self._view.row_for(row_key.value) if row_key.value is not None else None
        self.tooltip = (format_timestamp_full(row.get(field)) or None) if row else None


@register_category_view
class TaskListView(CategoryView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.TASK

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by task name -- the merged show_tasks +
        #: activity dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = _TaskListDataTable(
            self, id="detail-table-list", cursor_type="row"
        )
        table.zebra_stripes = True
        yield table

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that task's detail view, revealed in the
        tree -- see PipeListView's identical handler for the full reasoning."""
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectKind.TASK, self.scope.database, self.scope.schema, name)
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

        table = self.query_one("#detail-table-list", DataTable)
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
