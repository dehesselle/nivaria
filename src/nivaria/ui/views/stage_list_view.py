# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Category view for the "Stages (n)" node: an overview of every stage in a
schema, the same shape `table_list_view.py` uses.

Unlike Tables/Views (columns) or Pipes/Tasks (a SQL definition blob plus
multi-row history), a stage has nothing that needs its own per-object view, so
this list is the only place stages are shown in any detail. Every column but
one comes straight from `show_stages`, already mirrored for the tree's own
category count. "Last Altered" is the exception: `SHOW STAGES` has no such
timestamp, so it comes from INFORMATION_SCHEMA.STAGES in its own scope
(`stage_stats_scope`), needs a warehouse, and stays blank without a successful
pass, the same way the Tables list's Last DDL/Last Altered do.

`Type` is shown even though it is not the most interesting column on its own,
because `Storage Integration`/`URL` are blank for an internal stage -- the
same role `view_list_view.py`'s `Materialized` column plays for its own blank
Rows/Size cells: it is what explains the blank, not a cache miss.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from tcoda.widgets import DataTable
from textual.app import ComposeResult
from textual.widgets import Static

from nivaria.format import format_date, format_timestamp_minutes
from nivaria.models import ObjectType, Scope, stage_stats_scope
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
    "Type",
    "Storage Integration",
    "URL",
    "Created",
    "Last Altered",
    "Comment",
)

#: Which merged-row field a tooltip-bearing column's full value comes
#: from, keyed by that column's header. Every other column has no tooltip. See
#: table_list_view.py's identical map for the same reasoning.
_TOOLTIP_FIELDS = {"Created": "created_on", "Last Altered": "LAST_ALTERED"}


@register_category_view
class StageListView(CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.STAGE

    def compose(self) -> ComposeResult:
        yield Static("", id=LIST_NOTE)
        yield Static("", id=SOURCE_NOTE)
        yield HoverTooltipTable(self, _TOOLTIP_FIELDS, id=LIST_TABLE)

    def stats_scope(self) -> Scope:
        return stage_stats_scope(self.scope.database, self.scope.schema)

    def scopes(self) -> list[Scope]:
        return [self.scope, self.stats_scope()]

    def update_content(self) -> None:
        sync_list_note(
            self.query_one(f"#{LIST_NOTE}", Static),
            self.repo.entry(self.scope),
            "stages",
        )
        stats_scope = self.stats_scope()
        sync_source_note(
            self.query_one(f"#{SOURCE_NOTE}", Static),
            [source_line(self.repo.entry(stats_scope), "Last Altered")],
        )
        # Keyed by STAGE_NAME so a missing/failed stats pass just means an
        # empty dict here -- every row still renders from show_stages alone.
        stats_by_name = {
            row["STAGE_NAME"]: row for row in self.repo.raw_rows(stats_scope)
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
                    Text(row.get("type") or ""),
                    Text(row.get("storage_integration") or ""),
                    Text(row.get("url") or ""),
                    Text(format_date(row.get("created_on"))),
                    Text(format_timestamp_minutes(merged.get("LAST_ALTERED"))),
                    Text(row.get("comment") or ""),
                    key=name,
                )
