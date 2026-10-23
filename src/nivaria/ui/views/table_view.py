"""Detail view for tables: their columns, their grants and some of their rows.

Also hosts `ColumnsView`, the whole tabbed body (Columns, Data, Grants) --
`view_view.py` builds on it directly and adds a view's SQL, rather than
duplicating any of this for a view.

The Grants and Data tabs are fetched only while they are shown. Neither
`SHOW GRANTS ON <object>` nor an object's rows have a schema-wide form to
prime from, so every table or view is its own round trip, and opening one
already costs two (its columns and their tags). Fetching either on open as
well would add to every object the tree cursor passes over, for tabs most
visits never look at. Data also needs a warehouse, and wakes it -- and for a
view it runs the view's own query, which the preview's LIMIT does not
shorten when that query aggregates or sorts. That is what the preview's
timeout (`PoolGateway.select_data_preview`) is for."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from tcoda.widgets import DataTable
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable as TextualDataTable
from textual.widgets import Static, TabbedContent, TabPane

from nivaria.cache.repository import Repository
from nivaria.models import (
    CacheState,
    ObjectRef,
    ObjectType,
    Scope,
    column_tag_references_scope,
    columns_scope,
    data_preview_scope,
    object_grants_scope,
)
from nivaria.settings import CACHE_TTL, config
from nivaria.ui.views.base import ObjectView, register_view
from nivaria.ui.views.grants import compose_grants, paint_grants
from nivaria.ui.views.tables import PanelledTable, TabbedView

#: Ordering the table falls back to, and returns to whenever this (reused)
#: view is pointed at a different object -- see `show`.
_DEFAULT_SORT = "ordinal"

#: The Data tab's grid, and the line above it saying how much of the table
#: it is.
DATA_TABLE = "detail-data"
DATA_NOTE = "detail-data-note"

#: Longest a Data cell is shown, so one long VARCHAR or VARIANT cannot widen
#: its column past the screen.
_CELL_WIDTH = 40


def _data_cell(value: Any) -> Text:
    """One value from a cached preview row, as its cell shows it.

    NULL is spelled out and dimmed: a blank cell would read the same as an
    empty string. Anything that is not a string is shown as JSON, so a
    BOOLEAN reads `true` rather than Python's `True`.
    """
    if value is None:
        return Text("NULL", style="dim")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.splitlines())
    if len(text) > _CELL_WIDTH:
        text = text[: _CELL_WIDTH - 1] + "\u2026"
    return Text(text)


class ColumnsView(TabbedView, ObjectView):
    """Shared body for tables and views: Columns, Data and Grants,
    mnemonic switchable (`c`/`d`/`g`, hidden from the footer). A subclass
    adds tabs of its own after these through `compose_extra_tabs`.

    Rows and size and comment for the table itself (not its columns) are
    not shown here -- they're already visible one step earlier, as columns
    in TableListView's own list of every table in the schema.

    The column list sits in a `PanelledTable`: the panel below it shows the
    highlighted column's tags, one `TAG = VALUE` per line (a multi-value
    tag's values joined on that one line), the same mechanism `PipeView`/
    `TaskView` use for a history row's error.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("c", "show_tab('columns')", "Columns", show=False),
        Binding("d", "show_tab('data')", "Data", show=False),
        Binding("g", "show_tab('grants')", "Grants", show=False),
    ]

    #: Built in `compose`. Its `rows` double as the values the sort keys
    #: rank by: the table is handed a RowKey and looks the row up there, so
    #: a column sorts by what it means rather than by the text it renders
    #: -- Tags by its count, Ordinal as a number.
    columns_panel: PanelledTable
    #: Built in `compose`.
    grants_panel: PanelledTable

    def __init__(self, repo: Repository, ref: ObjectRef) -> None:
        super().__init__(repo, ref)
        #: The table sorts itself and keeps doing so; only being pointed at a
        #: different object puts it back to the default.
        self._reset_sort = True

    def show(self, ref: ObjectRef) -> None:
        self._reset_sort = True
        super().show(ref)

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-columns"):
            with TabPane("[$accent bold]C[/]olumns", id="tab-columns"):
                table = DataTable(id="detail-columns", cursor_type="row")
                table.zebra_stripes = True
                self.columns_panel = PanelledTable(table, self.detail_text)
                yield self.columns_panel
            with TabPane("[$accent bold]D[/]ata", id="tab-data"):
                yield Static("", id=DATA_NOTE)
                data: TextualDataTable[Text] = TextualDataTable(
                    id=DATA_TABLE, cursor_type="row"
                )
                data.zebra_stripes = True
                yield data
            with TabPane("[$accent bold]G[/]rants", id="tab-grants"):
                self.grants_panel = compose_grants()
                yield self.grants_panel
            yield from self.compose_extra_tabs()

    def compose_extra_tabs(self) -> ComposeResult:
        """Tabs a subclass adds after these, which sort after Grants."""
        yield from ()

    def columns_scope(self) -> Scope:
        return columns_scope(self.ref.database, self.ref.schema, self.ref.name)

    def column_tags_scope(self) -> Scope:
        return column_tag_references_scope(
            self.ref.database, self.ref.schema, self.ref.name
        )

    def grants_scope(self) -> Scope:
        return object_grants_scope(self.ref)

    def data_scope(self) -> Scope:
        return data_preview_scope(self.ref.database, self.ref.schema, self.ref.name)

    def _tab_shown(self, tab: str) -> bool:
        return self.is_mounted and self.query_one(TabbedContent).active == tab

    def scopes(self) -> list[Scope]:
        # Grants and Data only while their tab is up -- see the module
        # docstring. Listing one otherwise would have the staleness sweep
        # (which watches this) fetch it for a tab nobody is looking at, and
        # route its updates into a repaint of it.
        scopes = [self.columns_scope(), self.column_tags_scope()]
        if self._tab_shown("tab-grants"):
            scopes.append(self.grants_scope())
        if self._tab_shown("tab-data"):
            scopes.append(self.data_scope())
        return scopes

    def _rank(self, field: str, default: Any) -> Callable[[Any], Any]:
        """Rank a row by one of the values stashed in the panel's rows."""
        return lambda row_key: self.columns_panel.rows.get(row_key.value, {}).get(
            field, default
        )

    def _add_columns(self, table: DataTable) -> None:
        """Added once, not per repaint: sorting rewrites a header in place, so
        there is nothing here that has to be rebuilt to show an arrow.

        Nullable/Default/Comment stay unsortable -- nobody asked to sort by
        those.

        The headers are short to keep the table narrow, so each one whose
        cell is a single cached field tooltips that field's name -- the
        cache's `INFORMATION_SCHEMA.COLUMNS` vocabulary -- as proof the column
        is Snowflake's and not a term invented here. Type and Tags have none:
        Type is DESC TABLE's full rendering, not DATA_TYPE alone, and Tags
        is nivaria's own count, so no single Snowflake name would be true.
        """
        table.add_column(
            "Ordinal",
            key="ordinal",
            sortable=True,
            sort_key=self._rank("ordinal", 0),
            tooltip="ORDINAL_POSITION",
        )
        table.add_column(
            "Name",
            key="name",
            sortable=True,
            sort_key=self._rank("name", ""),
            tooltip="COLUMN_NAME",
        )
        table.add_column(
            "Type", key="type", sortable=True, sort_key=self._rank("type", "")
        )
        table.add_column("Nullable", tooltip="IS_NULLABLE")
        table.add_column("Default", tooltip="COLUMN_DEFAULT")
        table.add_column(
            "Tags", key="tags", sortable=True, sort_key=self._rank("tag_count", 0)
        )
        table.add_column("Comment", tooltip="COMMENT")

    def update_content(self) -> None:
        self._update_columns()
        if self._tab_shown("tab-grants"):
            self._update_grants()
        if self._tab_shown("tab-data"):
            self._update_data()

    def _update_columns(self) -> None:
        scope = self.columns_scope()
        self.ensure(scope, config.get(CACHE_TTL))
        tags_scope = self.column_tags_scope()
        self.ensure(tags_scope, config.get(CACHE_TTL))

        # Built before the rows -- both the Tags cell and the value the
        # Tags column sorts by come out of this one scan. Grouped by tag
        # rather than kept as one pair per row: a multi-value tag arrives as
        # one row per value, and counting rows would score one PII tag with
        # two values the same as two separate tags.
        tags_by_column: dict[str, dict[str, list[str]]] = {}
        for r in self.repo.raw_rows(tags_scope):
            tags_by_column.setdefault(r.get("COLUMN_NAME"), {}).setdefault(
                r.get("TAG_NAME") or "", []
            ).append(r.get("TAG_VALUE") or "")
        # A "0" only means something once the SELECT behind tags_scope has
        # actually completed -- before that (no warehouse yet, or a failed
        # pass), it would be a confident-looking guess, so the cell stays
        # blank instead. Same COMPLETE-gated distinction sync_list_note
        # draws between "positively fetched empty" and "not yet known".
        # Sorting still has to put an unconfirmed column somewhere, though,
        # so it sorts as a plain 0 even while its cell reads blank -- a
        # transient state that resolves the moment the scope completes.
        tags_confirmed = self.repo.entry(tags_scope).state is CacheState.COMPLETE

        table = self.query_one("#detail-columns", DataTable)
        if not table.columns:
            self._add_columns(table)
        if self._reset_sort:
            self._reset_sort = False
            sort, reverse = _DEFAULT_SORT, False
        else:
            sort, reverse = table.sort_column, table.sort_reverse

        with self.keeping_cursor(table):
            table.clear()
            rows = self.columns_panel.rows = {}
            for column in self.repo.columns(scope).rows:
                tags = tags_by_column.get(column.column_name, {})
                key = column.column_name
                # Rank values, not display values. The table is handed a RowKey
                # and reads these back, so rows go in in whatever order the cache
                # had them and the table puts them in order itself.
                rows[key] = {
                    "tags": tags,
                    "ordinal": column.ordinal_position,
                    "name": column.column_name.casefold(),
                    "type": column.desc_type.casefold(),
                    "tag_count": len(tags),
                }
                table.add_row(
                    Text(str(column.ordinal_position)),
                    Text(column.column_name),
                    Text(column.desc_type),
                    # YES/NO verbatim, as INFORMATION_SCHEMA.IS_NULLABLE
                    # reports it -- a column headed "Nullable" showing
                    # "NOT NULL" read backwards.
                    Text("YES" if column.is_nullable else "NO"),
                    Text(column.default or ""),
                    Text(str(len(tags)) if tags_confirmed else ""),
                    Text(column.comment or ""),
                    key=key,
                )
            # Sorted now rather than left to tcoda's on-idle resort: the
            # cursor is put back by row index as this block ends, and an
            # index taken before the sort would point at the wrong column.
            table.sort_by(sort, reverse=reverse)
        self.columns_panel.sync()

    def detail_text(self, row: dict[str, Any]) -> str | None:
        """A column's own tags, for the panel under the table."""
        tags = row.get("tags") or {}
        if not tags:
            return None
        return "\n".join(
            f"{name} = {', '.join(values)}" for name, values in tags.items()
        )

    def _update_grants(self) -> None:
        scope = self.grants_scope()
        self.ensure(scope, config.get(CACHE_TTL))
        paint_grants(self, self.grants_panel, self.repo.raw_rows(scope))

    def _update_data(self) -> None:
        scope = self.data_scope()
        self.ensure(scope, config.get(CACHE_TTL))
        rows = [json.loads(r["_values"]) for r in self.repo.raw_rows(scope)]
        table = self.query_one(f"#{DATA_TABLE}", TextualDataTable)
        with self.keeping_cursor(table):
            # Columns too, not only rows: the next table has its own.
            table.clear(columns=True)
            # Read off the first row, which keeps SELECT *'s column order. The
            # column list's cache could give an empty table a header too, but
            # it can be older than these rows and name other columns.
            names = list(rows[0]) if rows else []
            for name in names:
                table.add_column(Text(name))
            for number, row in enumerate(rows):
                table.add_row(
                    *(_data_cell(row.get(name)) for name in names), key=str(number)
                )
        self._sync_data_note(len(rows))

    def _sync_data_note(self, count: int) -> None:
        """Say how much of the table the grid is, or why it is empty.

        A capped preview is the normal case, not a warning, but it still
        says so: 100 rows look the same whether they are the whole table or
        a slice of it, and LIMIT without ORDER BY picks no particular slice.
        """
        note = self.query_one(f"#{DATA_NOTE}", Static)
        entry = self.repo.entry(self.data_scope())
        error = entry.state is CacheState.ERROR and entry.fetched_at is None
        empty = entry.state is CacheState.COMPLETE and count == 0
        capped = entry.truncated and count > 0
        if error:
            note.update(Text(f"Could not fetch: {entry.error}"))
        elif empty:
            note.update(Text(f"No rows in this {self.ref.type.value.lower()}."))
        elif capped:
            detail = self.repo.object_detail(self.ref) or {}
            total = detail.get("rows")
            of = f" of {total:,}" if isinstance(total, int) and total > count else ""
            note.update(Text(f"Showing {count:,}{of} rows, in no particular order."))
        note.set_class(error, "-error")
        note.display = error or empty or capped

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.pane.id == "tab-grants":
            self._update_grants()
        elif event.pane.id == "tab-data":
            self._update_data()


@register_view
class TableView(ColumnsView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.TABLE
