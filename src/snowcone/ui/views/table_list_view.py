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
from textual.app import ComposeResult
from textual.widgets import DataTable

from snowcone.cache.repository import Repository
from snowcone.format import format_bytes, format_date, format_timestamp_minutes
from snowcone.models import ObjectRef, ObjectType, Scope, table_stats_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.views.base import CategoryView, register_category_view
from snowcone.ui.views.tables import LIST_TABLE, HoverTooltipTable

_COLUMNS = ("Name", "Rows", "Size", "Created", "Last DDL", "Last Altered", "Comment")

#: Which merged-row field a tooltip-bearing column's full value comes
#: from, keyed by that column's header. Every other column has no tooltip.
_TOOLTIP_FIELDS = {
    "Created": "created_on",
    "Last DDL": "LAST_DDL",
    "Last Altered": "LAST_ALTERED",
}


@register_category_view
class TableListView(CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.TABLE

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by table name -- the merged show_tables +
        #: stats dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = HoverTooltipTable(
            self, _TOOLTIP_FIELDS, id=LIST_TABLE, cursor_type="row"
        )
        table.zebra_stripes = True
        yield table

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that table's detail view, revealed in the
        tree -- see PipeListView's identical handler for the full reasoning."""
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectType.TABLE, self.scope.database, self.scope.schema, name)
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

        table = self.query_one(f"#{LIST_TABLE}", DataTable)
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
