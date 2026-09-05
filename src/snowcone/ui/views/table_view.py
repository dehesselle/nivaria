"""Detail view for tables and views: their columns."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable, Label, Static

from snowcone.format import format_bytes
from snowcone.models import ObjectKind, Scope, columns_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.views.base import ObjectView, register_view

_COLUMNS = ("Column", "Type", "Null", "Default", "Comment")


class ColumnsView(ObjectView):
    """Shared body for the kinds whose detail is a column list."""

    def compose(self) -> ComposeResult:
        yield Static("", id="detail-summary")
        table: DataTable[Text] = DataTable(id="detail-columns", cursor_type="row")
        table.zebra_stripes = True
        yield table

    def columns_scope(self) -> Scope:
        return columns_scope(self.ref.database, self.ref.schema, self.ref.name)

    def scopes(self) -> list[Scope]:
        return [self.columns_scope()]

    def compose_context(self) -> ComposeResult:
        yield Label(self.ref.fqn, id="context-title")
        yield Static(self._facts(), id="context-detail")

    def update_content(self) -> None:
        scope = self.columns_scope()
        self.ensure(scope, config.get(CACHE_TTL))
        # The context bar above already carries the name; repeating it
        # here would spend a line of a small pane saying nothing new.
        self.query_one("#detail-summary", Static).update(self._facts())

        table = self.query_one("#detail-columns", DataTable)
        table.clear(columns=True)
        table.add_columns(*_COLUMNS)
        cached = self.repo.columns(scope)
        for column in cached.rows:
            table.add_row(
                Text(column.column_name),
                Text(column.data_type),
                Text("" if column.is_nullable else "NOT NULL"),
                Text(column.default or ""),
                Text(column.comment or ""),
            )

    def _facts(self) -> str:
        """Size and comment, without the name."""
        row = self.repo.object_detail(self.ref)
        if row is None:
            return "not cached"
        parts: list[str] = []
        if row.get("rows") is not None:
            parts.append(f"{row['rows']:,} rows")
        if row.get("bytes"):
            parts.append(format_bytes(row["bytes"]))
        if row.get("comment"):
            parts.append(str(row["comment"]))
        return "  ·  ".join(parts) or "no details cached"


@register_view
class TableView(ColumnsView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.TABLE


@register_view
class ViewView(ColumnsView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.VIEW
