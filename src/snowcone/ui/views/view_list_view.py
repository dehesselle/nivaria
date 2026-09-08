"""Category view for the "Views (n)" node: an overview of every view in a
schema, the same shape `table_list_view.py` uses.

The base columns (Name, Created, Comment) are sourced entirely from
`SHOW VIEWS`, already mirrored for the tree's own category count -- no new
fetch scope for those. Rows/Size and Last DDL/Last Altered are each a second
dual-source addition, for two different reasons `SHOW VIEWS` cannot supply
them itself:

* Rows/Size come from `SHOW MATERIALIZED VIEWS` (`materialized_view_stats_scope`),
  a genuinely different Snowflake command with its own column set -- SHOW
  VIEWS never returns row/byte stats, not even for materialized views. Only
  materialized views ever appear in this scope's rows, so a regular view
  simply has no match on merge -- no filtering needed, the same "missing
  means blank" shape `table_list_view.py`'s own stats merge already uses.
* Last DDL/Last Altered come from `view_stats_scope`, the same
  INFORMATION_SCHEMA.TABLES statement `table_list_view.py`'s own stats scope
  uses, just filtered to views instead of base tables -- see DESIGN.md's
  dual-source note. That SELECT needs a running warehouse, unlike
  Rows/Size's SHOW-sourced scope; a session with no warehouse, or any other
  query failure, just leaves those two columns blank.

Materialized is shown because it is the only thing that explains why most
rows' Rows/Size are blank: a regular view has neither, by Snowflake design,
not because of a cache miss.
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
from snowcone.format import (
    format_bytes,
    format_date,
    format_timestamp_full,
    format_timestamp_minutes,
)
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    Scope,
    materialized_view_stats_scope,
    view_stats_scope,
)
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.views.base import CategoryView, register_category_view

_COLUMNS = (
    "Name",
    "Rows",
    "Size",
    "Created",
    "Last DDL",
    "Last Altered",
    "Materialized",
    "Comment",
)

#: Which merged-row field a tooltip-bearing column's full value comes from,
#: by column index. Every other column has no tooltip. See
#: table_list_view.py's identical map for the same reasoning.
_TOOLTIP_FIELDS = {3: "created_on", 4: "LAST_DDL", 5: "LAST_ALTERED"}


class _ViewListDataTable(DataTable):
    """Tooltips the full-precision timestamp on hover over a truncated cell.

    Identical to table_list_view.py's `_TableListDataTable` -- see there.
    """

    def __init__(self, view: ViewListView, **kwargs: Any) -> None:
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
class ViewListView(CategoryView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.VIEW

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by view name -- the merged show_views +
        #: stats dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = _ViewListDataTable(
            self, id="detail-table-list", cursor_type="row"
        )
        table.zebra_stripes = True
        yield table

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that view's detail view, revealed in the
        tree -- see TableListView's identical handler for the full
        reasoning."""
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectKind.VIEW, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectSelected(ref))

    def stats_scope(self) -> Scope:
        return view_stats_scope(self.scope.database, self.scope.schema)

    def materialized_stats_scope(self) -> Scope:
        return materialized_view_stats_scope(self.scope.database, self.scope.schema)

    def scopes(self) -> list[Scope]:
        return [self.scope, self.stats_scope(), self.materialized_stats_scope()]

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        self.ensure(self.scope, ttl)
        stats_scope = self.stats_scope()
        self.ensure(stats_scope, ttl)
        materialized_scope = self.materialized_stats_scope()
        self.ensure(materialized_scope, ttl)
        # Keyed by TABLE_NAME/name so a missing/failed pass just means an
        # empty dict here -- every row still renders from show_views alone.
        stats_by_name = {
            row["TABLE_NAME"]: row for row in self.repo.raw_rows(stats_scope)
        }
        materialized_by_name = {
            row["name"]: row for row in self.repo.raw_rows(materialized_scope)
        }

        table = self.query_one("#detail-table-list", DataTable)
        table.clear(columns=True)
        table.add_columns(*_COLUMNS)
        self._rows = {}
        for row in self.repo.raw_rows(self.scope):
            name = row["name"]
            merged = {
                **row,
                **stats_by_name.get(name, {}),
                **materialized_by_name.get(name, {}),
            }
            self._rows[name] = merged
            table.add_row(
                Text(name),
                Text(f"{merged['rows']:,}" if merged.get("rows") is not None else ""),
                Text(format_bytes(merged.get("bytes"))),
                Text(format_date(row.get("created_on"))),
                Text(format_date(merged.get("LAST_DDL"))),
                Text(format_timestamp_minutes(merged.get("LAST_ALTERED"))),
                Text("Yes" if row.get("is_materialized") == "true" else ""),
                Text(row.get("comment") or ""),
                key=name,
            )
