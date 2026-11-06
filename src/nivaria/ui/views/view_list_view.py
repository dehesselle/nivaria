# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

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
  uses, just filtered to views instead of base tables -- see docs/DESIGN.md
  "A second source". That SELECT needs a running warehouse, unlike
  Rows/Size's SHOW-sourced scope; a session with no warehouse, or any other
  query failure, just leaves those two columns blank.

Materialized is shown because it is the only thing that explains why most
rows' Rows/Size are blank: a regular view has neither, by Snowflake design,
not because of a cache miss.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable, Static

from nivaria.format import (
    format_bytes,
    format_date,
    format_timestamp_minutes,
)
from nivaria.models import (
    ObjectType,
    Scope,
    materialized_view_stats_scope,
    view_stats_scope,
)
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

#: Which merged-row field a tooltip-bearing column's full value comes
#: from, keyed by that column's header. Every other column has no tooltip. See
#: table_list_view.py's identical map for the same reasoning.
_TOOLTIP_FIELDS = {
    "Created": "created_on",
    "Last DDL": "LAST_DDL",
    "Last Altered": "LAST_ALTERED",
}


@register_category_view
class ViewListView(CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.VIEW

    def compose(self) -> ComposeResult:
        yield Static("", id=LIST_NOTE)
        yield Static("", id=SOURCE_NOTE)
        yield HoverTooltipTable(self, _TOOLTIP_FIELDS, id=LIST_TABLE)

    def stats_scope(self) -> Scope:
        return view_stats_scope(self.scope.database, self.scope.schema)

    def materialized_stats_scope(self) -> Scope:
        return materialized_view_stats_scope(self.scope.database, self.scope.schema)

    def scopes(self) -> list[Scope]:
        return [self.scope, self.stats_scope(), self.materialized_stats_scope()]

    def update_content(self) -> None:
        sync_list_note(
            self.query_one(f"#{LIST_NOTE}", Static),
            self.repo.entry(self.scope),
            "views",
        )
        stats_scope = self.stats_scope()
        materialized_scope = self.materialized_stats_scope()
        sync_source_note(
            self.query_one(f"#{SOURCE_NOTE}", Static),
            [
                source_line(self.repo.entry(stats_scope), "Last DDL, Last Altered"),
                source_line(self.repo.entry(materialized_scope), "Rows, Size"),
            ],
        )
        # Keyed by TABLE_NAME/name so a missing/failed pass just means an
        # empty dict here -- every row still renders from show_views alone.
        stats_by_name = {
            row["TABLE_NAME"]: row for row in self.repo.raw_rows(stats_scope)
        }
        materialized_by_name = {
            row["name"]: row for row in self.repo.raw_rows(materialized_scope)
        }

        table = self.query_one(f"#{LIST_TABLE}", DataTable)
        with self.keeping_cursor(table):
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
                    Text(
                        f"{merged['rows']:,}" if merged.get("rows") is not None else ""
                    ),
                    Text(format_bytes(merged.get("bytes"))),
                    Text(format_date(row.get("created_on"))),
                    Text(format_date(merged.get("LAST_DDL"))),
                    Text(format_timestamp_minutes(merged.get("LAST_ALTERED"))),
                    Text("Yes" if row.get("is_materialized") == "true" else ""),
                    Text(row.get("comment") or ""),
                    key=name,
                )
