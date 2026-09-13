"""Detail view for tables: their columns.

Also hosts `ColumnsView`, the shared column-list body -- `view_view.py`
reuses it directly for its own Columns tab, rather than duplicating this
DataTable/scope/update_content logic for a view's columns."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import DataTable, Static

from snowcone.cache.repository import Repository
from snowcone.format import format_data_type
from snowcone.models import (
    CacheState,
    Column,
    ObjectRef,
    ObjectType,
    Scope,
    column_tag_references_scope,
    columns_scope,
)
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.views.base import ObjectView, register_view
from snowcone.ui.views.tables import ITEM_DETAIL, ItemDetailView

#: Header label paired with a stable key for the two sortable columns;
#: Type/Nullable/Default/Tags/Comment get auto-generated keys and stay
#: unsortable -- nobody asked to sort by those.
_COLUMNS: tuple[tuple[str, str] | str, ...] = (
    ("Ordinal", "ordinal"),
    ("Name", "name"),
    "Type",
    "Nullable",
    "Default",
    "Tags",
    "Comment",
)

_SORT_KEYS: dict[str, Callable[[Column], Any]] = {
    "ordinal": lambda c: c.ordinal_position,
    "name": lambda c: c.column_name.lower(),
}


class ColumnsView(ItemDetailView, ObjectView):
    """Shared body for the types whose detail is a column list.

    Rows and size and comment for the table itself (not its columns) are
    not shown here -- they're already visible one step earlier, as columns
    in TableListView's own list of every table in the schema.

    Also an `ItemDetailView`: the panel below the table shows the highlighted
    column's tags, one `TAG = VALUE` per line, the same mechanism `PipeView`/
    `TaskView` use for a history row's error.
    """

    detail_table_id = "detail-columns"

    def __init__(self, repo: Repository, ref: ObjectRef) -> None:
        super().__init__(repo, ref)
        #: Which header was last clicked, and in which direction. Reset to
        #: the declaration-order default whenever `show` points this (reused)
        #: view at a different object -- see `show` below.
        self._sort_field = "ordinal"
        self._sort_reverse = False
        self.detail_rows: dict[str, dict[str, Any]] = {}

    def show(self, ref: ObjectRef) -> None:
        self._sort_field = "ordinal"
        self._sort_reverse = False
        super().show(ref)

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = DataTable(id="detail-columns", cursor_type="row")
        table.zebra_stripes = True
        yield table
        yield Static("", id=ITEM_DETAIL)

    def columns_scope(self) -> Scope:
        return columns_scope(self.ref.database, self.ref.schema, self.ref.name)

    def column_tags_scope(self) -> Scope:
        return column_tag_references_scope(
            self.ref.database, self.ref.schema, self.ref.name
        )

    def scopes(self) -> list[Scope]:
        return [self.columns_scope(), self.column_tags_scope()]

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        field = event.column_key.value
        if field not in _SORT_KEYS:
            return
        if field == self._sort_field:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_field = field
            self._sort_reverse = False
        self.update_content()

    def update_content(self) -> None:
        scope = self.columns_scope()
        self.ensure(scope, config.get(CACHE_TTL))
        tags_scope = self.column_tags_scope()
        self.ensure(tags_scope, config.get(CACHE_TTL))

        table = self.query_one("#detail-columns", DataTable)
        table.clear(columns=True)
        table.add_columns(*_COLUMNS)
        cached = self.repo.columns(scope)
        rows = sorted(
            cached.rows, key=_SORT_KEYS[self._sort_field], reverse=self._sort_reverse
        )

        tags_by_column: dict[str, list[tuple[str, str]]] = {}
        for r in self.repo.raw_rows(tags_scope):
            tags_by_column.setdefault(r.get("COLUMN_NAME"), []).append(
                (r.get("TAG_NAME") or "", r.get("TAG_VALUE") or "")
            )
        # A "0" only means something once the SELECT behind tags_scope has
        # actually completed -- before that (no warehouse yet, or a failed
        # pass), it would be a confident-looking guess, so the cell stays
        # blank instead. Same COMPLETE-gated distinction sync_list_note
        # draws between "positively fetched empty" and "not yet known".
        tags_confirmed = self.repo.entry(tags_scope).state is CacheState.COMPLETE

        self.detail_rows = {}
        for column in rows:
            tags = tags_by_column.get(column.column_name, [])
            key = column.column_name
            self.detail_rows[key] = {"tags": tags}
            table.add_row(
                Text(str(column.ordinal_position)),
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
                # YES/NO verbatim, as INFORMATION_SCHEMA.IS_NULLABLE
                # reports it -- a column headed "Nullable" showing
                # "NOT NULL" read backwards.
                Text("YES" if column.is_nullable else "NO"),
                Text(column.default or ""),
                Text(str(len(tags)) if tags_confirmed else ""),
                Text(column.comment or ""),
                key=key,
            )
        self.sync_item_detail(table.cursor_row)

    def detail_text(self, row: dict[str, Any]) -> str | None:
        """A column's own tags, for the panel under the table."""
        tags = row.get("tags") or []
        if not tags:
            return None
        return "\n".join(f"{name} = {value}" for name, value in tags)


@register_view
class TableView(ColumnsView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.TABLE
