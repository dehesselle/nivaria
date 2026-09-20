"""Detail view for tables: their columns.

Also hosts `ColumnsView`, the shared column-list body -- `view_view.py`
reuses it directly for its own Columns tab, rather than duplicating this
DataTable/scope/update_content logic for a view's columns."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from tcw.widgets import DataTable
from textual.app import ComposeResult
from textual.widgets import Static

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

#: Ordering the table falls back to, and returns to whenever this (reused)
#: view is pointed at a different object -- see `show`.
_DEFAULT_SORT = "ordinal"


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
        #: Per-row values the sort keys rank by, keyed the same way the table
        #: keys its rows. The table is handed a RowKey and looks the row up
        #: here, so a column sorts by what it means rather than by the text
        #: it renders -- Tags by its count, Ordinal as a number.
        self.detail_rows: dict[str, dict[str, Any]] = {}
        #: The table sorts itself and keeps doing so; only being pointed at a
        #: different object puts it back to the default.
        self._reset_sort = True

    def show(self, ref: ObjectRef) -> None:
        self._reset_sort = True
        super().show(ref)

    def compose(self) -> ComposeResult:
        table = DataTable(id="detail-columns", cursor_type="row")
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

    def _rank(self, field: str, default: Any) -> Callable[[Any], Any]:
        """Rank a row by one of the values stashed in `detail_rows`."""
        return lambda row_key: self.detail_rows.get(row_key.value, {}).get(
            field, default
        )

    def _add_columns(self, table: DataTable) -> None:
        """Added once, not per repaint: sorting rewrites a header in place, so
        there is nothing here that has to be rebuilt to show an arrow.

        Nullable/Default/Comment stay unsortable -- nobody asked to sort by
        those.
        """
        table.add_column(
            "Ordinal", key="ordinal", sortable=True, sort_key=self._rank("ordinal", 0)
        )
        table.add_column(
            "Name", key="name", sortable=True, sort_key=self._rank("name", "")
        )
        table.add_column(
            "Type", key="type", sortable=True, sort_key=self._rank("type", "")
        )
        table.add_column("Nullable")
        table.add_column("Default")
        table.add_column(
            "Tags", key="tags", sortable=True, sort_key=self._rank("tag_count", 0)
        )
        table.add_column("Comment")

    def update_content(self) -> None:
        scope = self.columns_scope()
        self.ensure(scope, config.get(CACHE_TTL))
        tags_scope = self.column_tags_scope()
        self.ensure(tags_scope, config.get(CACHE_TTL))

        # Built before the rows -- both the Tags cell and the value the
        # Tags column sorts by come out of this one scan.
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
        # Sorting still has to put an unconfirmed column somewhere, though,
        # so it sorts as a plain 0 even while its cell reads blank -- a
        # transient state that resolves the moment the scope completes.
        tags_confirmed = self.repo.entry(tags_scope).state is CacheState.COMPLETE

        def _type_of(column: Column) -> str:
            return format_data_type(
                column.data_type,
                character_maximum_length=column.character_maximum_length,
                numeric_precision=column.numeric_precision,
                numeric_scale=column.numeric_scale,
                datetime_precision=column.datetime_precision,
            )

        table = self.query_one("#detail-columns", DataTable)
        if not table.columns:
            self._add_columns(table)
        table.clear()
        if self._reset_sort:
            self._reset_sort = False
            table.sort_by(_DEFAULT_SORT)

        self.detail_rows = {}
        for column in self.repo.columns(scope).rows:
            tags = tags_by_column.get(column.column_name, [])
            key = column.column_name
            # Rank values, not display values. The table is handed a RowKey
            # and reads these back, so rows go in in whatever order the cache
            # had them and the table puts them in order itself.
            self.detail_rows[key] = {
                "tags": tags,
                "ordinal": column.ordinal_position,
                "name": column.column_name.casefold(),
                "type": _type_of(column).casefold(),
                "tag_count": len(tags),
            }
            table.add_row(
                Text(str(column.ordinal_position)),
                Text(column.column_name),
                Text(_type_of(column)),
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
