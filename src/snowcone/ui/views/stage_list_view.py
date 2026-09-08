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
from textual import events
from textual.app import ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist

from snowcone.cache.repository import Repository
from snowcone.format import format_date, format_timestamp_full
from snowcone.models import ObjectKind, ObjectRef, Scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.views.base import CategoryView, register_category_view

_COLUMNS = ("Name", "Type", "Storage Integration", "URL", "Created", "Comment")

#: Which merged-row field a tooltip-bearing column's full value comes from,
#: by column index. Every other column has no tooltip. See
#: table_list_view.py's identical map for the same reasoning.
_TOOLTIP_FIELDS = {4: "created_on"}


class _StageListDataTable(DataTable):
    """Tooltips the full-precision timestamp on hover over a truncated cell.

    Identical to table_list_view.py's `_TableListDataTable` -- see there.
    """

    def __init__(self, view: StageListView, **kwargs: Any) -> None:
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
class StageListView(CategoryView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.STAGE

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by stage name -- the show_stages dict the
        #: hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = _StageListDataTable(
            self, id="detail-table-list", cursor_type="row"
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
        ref = ObjectRef(ObjectKind.STAGE, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectSelected(ref))

    def scopes(self) -> list[Scope]:
        return [self.scope]

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        self.ensure(self.scope, ttl)

        table = self.query_one("#detail-table-list", DataTable)
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
