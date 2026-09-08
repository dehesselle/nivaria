"""Category view for the "Pipes (n)" node: status and load activity.

Tabbed -- Status (one row per pipe: name, running/stopped, and
`invalid_reason` -- a stopped pipe is otherwise indistinguishable from a
healthy one) opens by default, alongside Load history (last load activity
per pipe). Status needs no new scope: `invalid_reason` is already mirrored
onto the same `SHOW PIPES` rows this view already reads for the Name column.

The base rows (every pipe that exists) come from `SHOW PIPES`, already
mirrored for the tree's own category count -- no new fetch scope for those.
Rows/Size/Loaded/Status/Target Table/Filename are this app's second set of
columns sourced from an ordinary `SELECT` instead of `SHOW` (see DESIGN.md's
dual-source note and `table_list_view.py`'s precedent): `SHOW PIPES` carries
no load-activity data at all. That SELECT (against
SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY) needs a running warehouse and elevated
privileges, unlike everything else in this view, so it lives in its own
scope (`pipe_activity_scope`) with its own freshness -- a session missing
either just leaves those columns blank (see `Repository.fail_pass`) while
the SHOW-sourced Name column keeps rendering whatever was cached before.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.widgets import DataTable, TabbedContent, TabPane
from textual.widgets.data_table import CellDoesNotExist

from snowcone.cache.repository import Repository
from snowcone.format import (
    format_bytes,
    format_timestamp_full,
    format_timestamp_minutes,
)
from snowcone.models import ObjectKind, ObjectRef, Scope, pipe_activity_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.views.base import CategoryView, register_category_view

_STATUS_COLUMNS = ("Name", "Status", "Invalid Reason")
_HISTORY_COLUMNS = (
    "Name",
    "Rows",
    "Size",
    "Loaded",
    "Status",
    "Target Table",
    "Filename",
)


#: Which merged-row field a tooltip-bearing column's full value comes from,
#: by column index. Every other column has no tooltip.
_TOOLTIP_FIELDS = {3: "LAST_LOAD_TIME"}


class _PipeListDataTable(DataTable):
    """Tooltips the full-precision "Loaded" timestamp on hover.

    Same pattern as `table_list_view._TableListDataTable` -- duplicated
    rather than shared, matching that class's own precedent (it cites
    snakecam's near-identical widget rather than sharing code with it).
    """

    def __init__(self, view: PipeListView, **kwargs: Any) -> None:
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
class PipeListView(CategoryView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.PIPE

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("s", "show_tab('status')", "Status", show=False),
        Binding("l", "show_tab('history')", "Load history", show=False),
    ]

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by pipe name -- the merged show_pipes +
        #: activity dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-status"):
            with TabPane("[$accent bold]S[/]tatus", id="tab-status"):
                status: DataTable[Text] = DataTable(
                    id="pipe-list-status", cursor_type="row"
                )
                status.zebra_stripes = True
                status.add_columns(*_STATUS_COLUMNS)
                yield status
            with TabPane("[$accent bold]L[/]oad history", id="tab-history"):
                history: DataTable[Text] = _PipeListDataTable(
                    self, id="detail-table-list", cursor_type="row"
                )
                history.zebra_stripes = True
                yield history

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = f"tab-{tab}"

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that pipe's detail view, revealed in the tree.

        Reuses the exact path a search hit already takes (`ObjectSelected` ->
        `MainScreen._select` -> `tree.reveal` + `detail.show`), so this needs
        no new message and no MainScreen change.
        """
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectKind.PIPE, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectSelected(ref))

    def activity_scope(self) -> Scope:
        return pipe_activity_scope(self.scope.database, self.scope.schema)

    def scopes(self) -> list[Scope]:
        return [self.scope, self.activity_scope()]

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        self.ensure(self.scope, ttl)
        activity_scope = self.activity_scope()
        self.ensure(activity_scope, ttl)
        # The cache already holds at most one row per pipe -- queries.
        # pipe_activity's QUALIFY keeps only each pipe's latest load. Keyed
        # by PIPE_NAME so a missing/failed activity pass just means an empty
        # dict here -- every row still renders from show_pipes alone.
        activity_by_name: dict[str, dict[str, Any]] = {
            name: row
            for row in self.repo.raw_rows(activity_scope)
            if (name := row.get("PIPE_NAME"))
        }

        status_table = self.query_one("#pipe-list-status", DataTable)
        status_table.clear()
        history_table = self.query_one("#detail-table-list", DataTable)
        history_table.clear(columns=True)
        history_table.add_columns(*_HISTORY_COLUMNS)
        self._rows = {}
        for row in self.repo.raw_rows(self.scope):
            name = row["name"]
            merged = {**row, **activity_by_name.get(name, {})}
            self._rows[name] = merged
            row_count = merged.get("ROW_COUNT")
            history_table.add_row(
                Text(name),
                Text(f"{row_count:,}" if row_count is not None else ""),
                Text(format_bytes(merged.get("FILE_SIZE"))),
                Text(format_timestamp_minutes(merged.get("LAST_LOAD_TIME"))),
                Text(merged.get("STATUS") or ""),
                Text(merged.get("TABLE_NAME") or ""),
                Text(merged.get("FILE_NAME") or ""),
                key=name,
            )
            invalid_reason = row.get("invalid_reason") or ""
            pipe_status = "stopped" if invalid_reason else "running"
            status_table.add_row(
                Text(name), Text(pipe_status), Text(invalid_reason), key=name
            )
