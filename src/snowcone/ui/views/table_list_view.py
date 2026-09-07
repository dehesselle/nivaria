"""Category view for the "Tables (n)" node: an overview of every table in a
schema, the way Snowsight's own table list does it.

The base columns are sourced entirely from `SHOW TABLES`, already mirrored
for the tree's own category count -- no new fetch scope for those. "Last
DDL" and "Last Altered" are this app's first columns sourced from an
ordinary `SELECT` against INFORMATION_SCHEMA.TABLES instead of `SHOW` (see
DESIGN.md's dual-source note): `SHOW TABLES` carries no equivalent
timestamp. That SELECT needs a running warehouse, unlike everything else in
this view, so it lives in its own scope (`table_stats_scope`) with its own
freshness -- a session with no warehouse, or any other query failure, just
leaves those two columns blank (see `Repository.fail_pass`) while the
SHOW-sourced columns keep rendering whatever was cached before.
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
    format_date,
    format_timestamp_full,
    format_timestamp_minutes,
)
from snowcone.models import ObjectKind, ObjectRef, Scope, table_stats_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.views.base import CategoryView, register_category_view

_COLUMNS = ("Name", "Rows", "Size", "Created", "Last DDL", "Last Altered", "Comment")

#: Which merged-row field a tooltip-bearing column's full value comes from,
#: by column index. Every other column has no tooltip.
_TOOLTIP_FIELDS = {3: "created_on", 4: "LAST_DDL", 5: "LAST_ALTERED"}


class _TableListDataTable(DataTable):
    """Tooltips the full-precision timestamp on hover over a truncated cell.

    Takes the owning view directly, the same pattern snakecam's
    PipelinesTable/JobsTable use (../snakecam/src/snakecam/screens/widgets.py)
    -- so the row data it needs on hover comes from the same merged rows the
    view just painted, without querying the cache again.
    """

    def __init__(self, view: TableListView, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._view = view

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        # Textual dispatches either the underscored or the bare form per
        # class, not both, and DataTable's own tooltip handling needs this
        # one -- see watch_hover_coordinate below.
        super()._on_mouse_move(event)
        # DataTable leaves hover_coordinate untouched when the mouse moves
        # over filler space that carries no row/column meta, so a tooltip
        # set while over a real cell would otherwise never get the reactive
        # change needed to clear it here.
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
class TableListView(CategoryView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.TABLE

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by table name -- the merged show_tables +
        #: stats dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = _TableListDataTable(
            self, id="detail-table-list", cursor_type="row"
        )
        table.zebra_stripes = True
        yield table

    def compose_context(self) -> ComposeResult:
        yield Label(f"{self.scope.database}.{self.scope.schema}", id="context-title")
        yield Static("Tables", id="context-detail")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that table's detail view, revealed in the
        tree -- see PipeListView's identical handler for the full reasoning."""
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectKind.TABLE, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectSelected(ref))

    def stats_scope(self) -> Scope:
        return table_stats_scope(self.scope.database, self.scope.schema)

    def scopes(self) -> list[Scope]:
        return [self.scope, self.stats_scope()]

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        self.ensure(self.scope, ttl)
        stats_scope = self.stats_scope()
        self.ensure(stats_scope, ttl)
        # Keyed by TABLE_NAME so a missing/failed stats pass just means an
        # empty dict here -- every row still renders from show_tables alone.
        stats_by_name = {
            row["TABLE_NAME"]: row for row in self.repo.raw_rows(stats_scope)
        }

        table = self.query_one("#detail-table-list", DataTable)
        table.clear(columns=True)
        table.add_columns(*_COLUMNS)
        self._rows = {}
        for row in self.repo.raw_rows(self.scope):
            name = row["name"]
            merged = {**row, **stats_by_name.get(name, {})}
            self._rows[name] = merged
            table.add_row(
                Text(name),
                Text(f"{row['rows']:,}" if row.get("rows") is not None else ""),
                Text(format_bytes(row.get("bytes"))),
                Text(format_date(row.get("created_on"))),
                Text(format_date(merged.get("LAST_DDL"))),
                Text(format_timestamp_minutes(merged.get("LAST_ALTERED"))),
                Text(row.get("comment") or ""),
                key=name,
            )
