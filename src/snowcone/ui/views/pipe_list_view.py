"""Category view for the "Pipes (n)" node: last load activity per pipe.

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
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Label, Static
from textual.widgets.data_table import CellDoesNotExist

from snowcone.cache.repository import Repository
from snowcone.format import (
    format_bytes,
    format_timestamp_full,
    format_timestamp_minutes,
)
from snowcone.models import ObjectKind, Scope, pipe_activity_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.views.base import CategoryView, register_category_view

_COLUMNS = ("Name", "Rows", "Size", "Loaded", "Status", "Target Table", "Filename")

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

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by pipe name -- the merged show_pipes +
        #: activity dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = _PipeListDataTable(
            self, id="detail-table-list", cursor_type="row"
        )
        table.zebra_stripes = True
        yield table

    def compose_context(self) -> ComposeResult:
        yield Label(f"{self.scope.database}.{self.scope.schema}", id="context-title")
        yield Static("Pipes", id="context-detail")

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
        # Keyed by PIPE_NAME so a missing/failed activity pass just means an
        # empty dict here -- every row still renders from show_pipes alone.
        activity_by_name = {
            row["PIPE_NAME"]: row for row in self.repo.raw_rows(activity_scope)
        }

        table = self.query_one("#detail-table-list", DataTable)
        table.clear(columns=True)
        table.add_columns(*_COLUMNS)
        self._rows = {}
        for row in self.repo.raw_rows(self.scope):
            name = row["name"]
            merged = {**row, **activity_by_name.get(name, {})}
            self._rows[name] = merged
            row_count = merged.get("ROW_COUNT")
            table.add_row(
                Text(name),
                Text(f"{row_count:,}" if row_count is not None else ""),
                Text(format_bytes(merged.get("FILE_SIZE"))),
                Text(format_timestamp_minutes(merged.get("LAST_LOAD_TIME"))),
                Text(merged.get("STATUS") or ""),
                Text(merged.get("TABLE_NAME") or ""),
                Text(merged.get("FILE_NAME") or ""),
                key=name,
            )
