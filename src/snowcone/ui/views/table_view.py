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

#: Header label paired with a stable key for the four sortable columns;
#: Nullable/Default/Comment get auto-generated keys and stay unsortable --
#: nobody asked to sort by those.
_COLUMNS: tuple[tuple[str, str] | str, ...] = (
    ("Ordinal", "ordinal"),
    ("Name", "name"),
    ("Type", "type"),
    "Nullable",
    "Default",
    ("Tags", "tags"),
    "Comment",
)

_SORTABLE_FIELDS: frozenset[str] = frozenset({"ordinal", "name", "type", "tags"})

#: Direction shown on the active sort column's header -- value direction,
#: not click direction (the common spreadsheet/grid convention).
_SORT_ASCENDING = "▲"
_SORT_DESCENDING = "▼"
#: Shown instead, on every other sortable header, so a column being
#: clickable doesn't have to be discovered by trial and error.
_SORT_INACTIVE = "·"


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
        if field not in _SORTABLE_FIELDS:
            return
        if field == self._sort_field:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_field = field
            self._sort_reverse = False
        self.update_content()

    def _columns(self) -> tuple[str | tuple[str, str], ...]:
        """`_COLUMNS`, with a direction arrow appended to the current sort
        column's header and a plain dot on every other sortable one (every
        `(label, key)` spec is sortable -- see `_COLUMNS`), so a column
        being clickable is visible before it's ever clicked. Decoration
        only -- the key stays untouched, so
        `on_data_table_header_selected`'s lookup is unaffected, and it's
        rebuilt fresh every call so switching columns reverts the previous
        one to the dot for free."""
        arrow = _SORT_DESCENDING if self._sort_reverse else _SORT_ASCENDING
        columns: list[str | tuple[str, str]] = []
        for spec in _COLUMNS:
            if isinstance(spec, tuple):
                label, key = spec
                indicator = arrow if key == self._sort_field else _SORT_INACTIVE
                columns.append((f"{label} {indicator}", key))
            else:
                columns.append(spec)
        return tuple(columns)

    def update_content(self) -> None:
        scope = self.columns_scope()
        self.ensure(scope, config.get(CACHE_TTL))
        tags_scope = self.column_tags_scope()
        self.ensure(tags_scope, config.get(CACHE_TTL))

        # Built before sorting -- a "tags" sort needs the count, and this
        # scan of the tags scope is its only source.
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

        # Local, not module-level: "tags" closes over tags_by_column, which
        # only exists once this scope's rows are in hand.
        sort_keys: dict[str, Callable[[Column], Any]] = {
            "ordinal": lambda c: c.ordinal_position,
            "name": lambda c: c.column_name.lower(),
            "type": lambda c: _type_of(c).lower(),
            "tags": lambda c: len(tags_by_column.get(c.column_name, [])),
        }

        table = self.query_one("#detail-columns", DataTable)
        table.clear(columns=True)
        table.add_columns(*self._columns())
        cached = self.repo.columns(scope)
        rows = sorted(
            cached.rows, key=sort_keys[self._sort_field], reverse=self._sort_reverse
        )

        self.detail_rows = {}
        for column in rows:
            tags = tags_by_column.get(column.column_name, [])
            key = column.column_name
            self.detail_rows[key] = {"tags": tags}
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
