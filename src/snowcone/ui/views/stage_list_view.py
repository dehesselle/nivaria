"""Category view for the "Stages (n)" node: an overview of every stage in a
schema, the same shape `table_list_view.py` uses.

Unlike Tables/Views (columns) or Pipes/Tasks (a SQL definition blob plus
multi-row history), a stage's `SHOW STAGES` row is the whole picture -- there
is nothing that needs its own per-object view, so this list is the only
place stages are shown in any detail, and no new fetch scope is needed: every
column comes straight from `show_stages`, already mirrored for the tree's own
category count.

`Type` is shown even though it is not the most interesting column on its own,
because `Storage Integration`/`URL` are blank for an internal stage -- the
same role `view_list_view.py`'s `Materialized` column plays for its own blank
Rows/Size cells: it is what explains the blank, not a cache miss.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable, Static

from snowcone.cache.repository import Repository
from snowcone.format import format_date
from snowcone.models import ObjectRef, ObjectType, Scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectOpened
from snowcone.ui.views.base import CategoryView, register_category_view
from snowcone.ui.views.tables import (
    LIST_NOTE,
    LIST_TABLE,
    HoverTooltipTable,
    sync_list_note,
)

_COLUMNS = ("Name", "Type", "Storage Integration", "URL", "Created", "Comment")

#: Which merged-row field a tooltip-bearing column's full value comes
#: from, keyed by that column's header. Every other column has no tooltip. See
#: table_list_view.py's identical map for the same reasoning.
_TOOLTIP_FIELDS = {"Created": "created_on"}


@register_category_view
class StageListView(CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.STAGE

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by stage name -- the show_stages dict the
        #: hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id=LIST_NOTE)
        table: DataTable[Text] = HoverTooltipTable(
            self, _TOOLTIP_FIELDS, id=LIST_TABLE, cursor_type="row"
        )
        table.zebra_stripes = True
        yield table

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that stage's detail view, revealed in the
        tree -- see TableListView's identical handler for the full
        reasoning."""
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectType.STAGE, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectOpened(ref))

    def scopes(self) -> list[Scope]:
        return [self.scope]

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        self.ensure(self.scope, ttl)
        sync_list_note(
            self.query_one(f"#{LIST_NOTE}", Static),
            self.repo.entry(self.scope),
            "stages",
        )

        table = self.query_one(f"#{LIST_TABLE}", DataTable)
        with self.keeping_cursor(table):
            table.clear(columns=True)
            table.add_columns(*_COLUMNS)
            self._rows = {}
            for row in self.repo.raw_rows(self.scope):
                name = row["name"]
                self._rows[name] = row
                table.add_row(
                    Text(name),
                    Text(row.get("type") or ""),
                    Text(row.get("storage_integration") or ""),
                    Text(row.get("url") or ""),
                    Text(format_date(row.get("created_on"))),
                    Text(row.get("comment") or ""),
                    key=name,
                )
