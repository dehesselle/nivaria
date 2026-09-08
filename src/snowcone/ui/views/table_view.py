"""Detail view for tables: their columns.

Also hosts `ColumnsView`, the shared column-list body -- `view_view.py`
reuses it directly for its own Columns tab, rather than duplicating this
DataTable/scope/update_content logic for a view's columns."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable

from snowcone.format import format_data_type
from snowcone.models import ObjectKind, Scope, columns_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.views.base import ObjectView, register_view

_COLUMNS = ("Column", "Type", "Null", "Default", "Comment")


class ColumnsView(ObjectView):
    """Shared body for the kinds whose detail is a column list.

    Rows and size and comment for the table itself (not its columns) are
    not shown here -- they're already visible one step earlier, as columns
    in TableListView's own list of every table in the schema.
    """

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = DataTable(id="detail-columns", cursor_type="row")
        table.zebra_stripes = True
        yield table

    def columns_scope(self) -> Scope:
        return columns_scope(self.ref.database, self.ref.schema, self.ref.name)

    def scopes(self) -> list[Scope]:
        return [self.columns_scope()]

    def update_content(self) -> None:
        scope = self.columns_scope()
        self.ensure(scope, config.get(CACHE_TTL))

        table = self.query_one("#detail-columns", DataTable)
        table.clear(columns=True)
        table.add_columns(*_COLUMNS)
        cached = self.repo.columns(scope)
        for column in cached.rows:
            table.add_row(
                Text(column.column_name),
                Text(
                    format_data_type(
                        column.data_type,
                        character_maximum_length=column.character_maximum_length,
                        numeric_precision=column.numeric_precision,
                        numeric_scale=column.numeric_scale,
                        datetime_precision=column.datetime_precision,
                    )
                ),
                Text("" if column.is_nullable else "NOT NULL"),
                Text(column.default or ""),
                Text(column.comment or ""),
            )


@register_view
class TableView(ColumnsView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.TABLE
