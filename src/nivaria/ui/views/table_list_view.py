# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Category view for the "Tables (n)" node: an overview of every table in a
schema, the way Snowsight's own table list does it.

The base columns are sourced entirely from `SHOW TABLES`, already mirrored
for the tree's own category count -- no new fetch scope for those. "Last
DDL" and "Last Altered" are this app's first columns sourced from an
ordinary `SELECT` against INFORMATION_SCHEMA.TABLES instead of `SHOW` (see
docs/DESIGN.md "A second source"): `SHOW TABLES` carries no equivalent
timestamp. That SELECT needs a running warehouse, unlike everything else in
this view, so it lives in its own scope (`table_stats_scope`) with its own
freshness -- a session with no warehouse, or any other query failure, just
leaves those two columns blank (see `Repository.fail_pass`) while the
SHOW-sourced columns keep rendering whatever was cached before.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from tcoda.widgets import DataTable
from textual.app import ComposeResult
from textual.widgets import Static

from nivaria.format import format_bytes, format_date, format_timestamp_minutes
from nivaria.models import ObjectType, Scope, table_stats_scope
from nivaria.ui.views.base import CategoryView, register_category_view
from nivaria.ui.views.tables import (
    LIST_NOTE,
    LIST_TABLE,
    SOURCE_NOTE,
    HoverTooltipTable,
    source_line,
    sync_list_note,
    sync_source_note,
)

_COLUMNS = ("Name", "Rows", "Size", "Created", "Last DDL", "Last Altered", "Comment")

#: Which merged-row field holds the raw value behind a shortened column, keyed
#: by that column's header. Every other column has no tooltip.
_TOOLTIP_FIELDS = {
    "Rows": "rows",
    "Size": "bytes",
    "Created": "created_on",
    "Last DDL": "LAST_DDL",
    "Last Altered": "LAST_ALTERED",
}


@register_category_view
class TableListView(CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.TABLE

    def compose(self) -> ComposeResult:
        yield Static("", id=LIST_NOTE)
        yield Static("", id=SOURCE_NOTE)
        yield HoverTooltipTable(self, _TOOLTIP_FIELDS, id=LIST_TABLE)

    def stats_scope(self) -> Scope:
        return table_stats_scope(self.scope.database, self.scope.schema)

    def scopes(self) -> list[Scope]:
        return [self.scope, self.stats_scope()]

    def update_content(self) -> None:
        sync_list_note(
            self.query_one(f"#{LIST_NOTE}", Static),
            self.repo.entry(self.scope),
            "tables",
        )
        stats_scope = self.stats_scope()
        sync_source_note(
            self.query_one(f"#{SOURCE_NOTE}", Static),
            [source_line(self.repo.entry(stats_scope), "Last DDL, Last Altered")],
        )
        # Keyed by TABLE_NAME so a missing/failed stats pass just means an
        # empty dict here -- every row still renders from show_tables alone.
        stats_by_name = {
            row["TABLE_NAME"]: row for row in self.repo.raw_rows(stats_scope)
        }

        table = self.query_one(f"#{LIST_TABLE}", DataTable)
        with self.keeping_cursor(table):
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
