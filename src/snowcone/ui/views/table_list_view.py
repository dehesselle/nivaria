"""Category view for the "Tables (n)" node: an overview of every table in a
schema, the way Snowsight's own table list does it.

Sourced entirely from `SHOW TABLES`, already mirrored for the tree's own
category count -- no new fetch scope. There is deliberately no last-DDL or
last-DML column: the real `SHOW TABLES` result carries only `created_on` (see
`tests/test_repository.py`'s verified-column test), and getting a true
"last altered" value would mean `INFORMATION_SCHEMA.TABLES`, which needs a
running warehouse -- the whole reason this app uses `SHOW` everywhere.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable, Label, Static

from snowcone.format import format_bytes
from snowcone.models import ObjectKind, Scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.views.base import CategoryView, register_category_view

_COLUMNS = ("Name", "Rows", "Size", "Created", "Comment")


@register_category_view
class TableListView(CategoryView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.TABLE

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = DataTable(id="detail-table-list", cursor_type="row")
        table.zebra_stripes = True
        yield table

    def compose_context(self) -> ComposeResult:
        yield Label(f"{self.scope.database}.{self.scope.schema}", id="context-title")
        yield Static("Tables", id="context-detail")

    def scopes(self) -> list[Scope]:
        return [self.scope]

    def update_content(self) -> None:
        self.ensure(self.scope, config.get(CACHE_TTL))

        table = self.query_one("#detail-table-list", DataTable)
        table.clear(columns=True)
        table.add_columns(*_COLUMNS)
        for row in self.repo.raw_rows(self.scope):
            table.add_row(
                Text(row["name"]),
                Text(f"{row['rows']:,}" if row.get("rows") is not None else ""),
                Text(format_bytes(row.get("bytes"))),
                Text(row.get("created_on") or ""),
                Text(row.get("comment") or ""),
            )
