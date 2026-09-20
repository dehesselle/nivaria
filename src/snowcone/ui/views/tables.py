"""Shared building blocks for the detail views.

Three shapes every view here used to write out for itself, and that had each
already drifted:

* **`HoverTooltipTable`** -- a `DataTable` that shows a cell's full-precision
  value on hover, once duplicated verbatim in five list views. Each copy
  carried a `{column index: field}` map, so reordering that view's `_COLUMNS`
  silently pointed its tooltips at the wrong column. This one maps *column
  headers* to fields and resolves the index from the live table, so there is
  no ordering to keep in step.
* **`TabbedView`** -- the two-line `action_show_tab`, once repeated in five
  views.
* **`ItemDetailView`** -- a panel below a table that describes whichever row
  is highlighted, hidden when that row has nothing to say. `HistoryView`
  (below) is its first consumer, for `PipeView`/`TaskView`'s load/run
  errors; `ColumnsView` (`table_view.py`) is its second, for a column's
  tags. Only `detail_text` and which table to watch differ per view --
  this used to be baked into `HistoryView` alone, under names
  (`HISTORY_DETAIL`, `history_rows`) that made it look error-specific when
  it never actually was.
* **`HistoryView`** -- adds a two-source history table on top of
  `ItemDetailView`, plus the "capped/empty/errored" note above it.
  `PipeView` and `TaskView` had ~40 identical lines each; only the message
  a row wants to show actually differs, which is the one method a
  subclass supplies.

The widget ids are shared too, and deliberately: exactly one detail view is
mounted at a time (see `DetailPane`), so `#detail-history` means "whichever
history table is on screen". The per-view ids these replaced had already
cost something real: `app.tcss` styled the pipe view's own history table,
detail panel and definition pane, and the task view -- a later copy of all
three, under its own ids -- silently picked up none of it.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar, Protocol

from rich.text import Text
from tcw.widgets import DataTable
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.widgets import DataTable as TextualDataTable
from textual.widgets import Static, TabbedContent
from textual.widgets.data_table import CellDoesNotExist, ColumnKey, RowKey

from snowcone.cache.repository import Entry, Repository
from snowcone.format import format_timestamp_full
from snowcone.models import CacheState, Scope

#: The one list/table in whichever detail view is mounted.
LIST_TABLE = "detail-list"
#: The line above LIST_TABLE saying so when its scope was fetched and came
#: back empty, or errored with nothing to fall back on -- see
#: `sync_list_note`.
LIST_NOTE = "detail-list-note"
#: A single object's history table, and the panel describing its cursor row.
HISTORY_TABLE = "detail-history"
#: The panel describing whichever row is highlighted in whichever table an
#: `ItemDetailView` is watching -- a history table's load/run error, or (see
#: `ColumnsView`) a column's tags. Shared across mutually-exclusive views the
#: same way HISTORY_TABLE/LIST_TABLE/DEFINITION are -- see the module
#: docstring.
ITEM_DETAIL = "detail-item"
#: The line above a history table admitting that it is not the whole
#: history -- see `HistoryView.sync_history_note`.
HISTORY_NOTE = "detail-history-note"
#: A single object's defining SQL.
DEFINITION = "detail-definition"


def sync_list_note(note: Static, entry: Entry, noun: str) -> None:
    """Say so when a category's primary scope was positively fetched and
    came back with nothing, or its only fetch attempt errored with no prior
    success to fall back on -- never when it simply has not been fetched
    yet. `EMPTY` (never touched) and `PARTIAL` (mid-refresh) both stay
    silent rather than guess; only `COMPLETE` with zero rows, or `ERROR`
    with no prior `fetched_at`, say anything. Same toggle-display,
    recompute-in-full-every-repaint shape as `HistoryView.sync_history_note`.
    """
    error = entry.state is CacheState.ERROR and entry.fetched_at is None
    empty = entry.state is CacheState.COMPLETE and entry.row_count == 0
    if error:
        note.update(Text(f"Could not fetch: {entry.error}"))
    elif empty:
        note.update(Text(f"No {noun} in this schema."))
    # Assigned unconditionally, not only in the branches above, so a note
    # widget reused across CategoryView.show() (switching schemas without
    # remounting) never carries a stale class or visibility from the
    # previous schema's paint.
    note.set_class(error, "-error")
    note.display = error or empty


class RowSource(Protocol):
    """What a `HoverTooltipTable` needs from the view that owns it: the row
    it just painted under a given key, so hovering costs no cache read."""

    def row_for(self, key: str) -> dict[str, Any] | None: ...


class HoverTooltipTable(DataTable):
    """Tooltips a cell's full-precision timestamp on hover.

    Takes the owning view directly, the same pattern snakecam's
    PipelinesTable/JobsTable use (../snakecam/src/snakecam/screens/widgets.py)
    -- so the row data it needs on hover comes from the same rows the view
    just painted, without querying the cache again.

    `tooltips` maps a column *key* to the merged-row field holding its
    untruncated value, and `add_column` below keys a column by its header --
    so a view can reorder its columns without mis-aiming its own tooltips,
    and the mapping still holds once a sortable header grows an indicator.
    """

    def __init__(
        self, view: RowSource, tooltips: dict[str, str], **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._view = view
        self._tooltips = tooltips

    def add_column(self, label, *, key: str | None = None, **kwargs: Any):
        # Key defaults to the header text. A tooltip names its column by key,
        # and a key survives the label growing a sort indicator -- which the
        # header text this used to match on would not.
        if key is None and isinstance(label, str):
            key = label
        return super().add_column(label, key=key, **kwargs)

    def get_cell_tooltip(self, row_key: RowKey, column_key: ColumnKey) -> str | None:
        field = self._tooltips.get(column_key.value)
        if field is None:
            return None
        row = self._view.row_for(row_key.value) if row_key.value is not None else None
        return (format_timestamp_full(row.get(field)) or None) if row else None


class TabbedView:
    """Mnemonic tab switching, for the views whose body is a `TabbedContent`.

    A mixin rather than a base class: the views that need it already inherit
    from `ObjectView`, `CategoryView` or `_ScopedView`, and what they share is
    only this one action.
    """

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = f"tab-{tab}"  # type: ignore[attr-defined]


class ItemDetailView:
    """A panel below a table, describing whichever row is highlighted.

    `HistoryView` (for `PipeView`/`TaskView`'s load/run errors) and
    `ColumnsView` (for a column's tags) both merge/list rows into one table
    and then want to say something about the cursor row that does not fit in
    a cell. Everything about *that* is the same across both; only
    `detail_text` and which table to watch (`detail_table_id`) differ.
    """

    #: The id of the DataTable this panel watches -- set by the subclass,
    #: since each view's own table has its own id (HISTORY_TABLE,
    #: "detail-columns", ...).
    detail_table_id: ClassVar[str]

    #: This paint's rows, keyed by the row key stamped into the table --
    #: looked back up when a row is highlighted. Set by the subclass on
    #: every repaint.
    detail_rows: dict[str, dict[str, Any]]

    @abstractmethod
    def detail_text(self, row: dict[str, Any]) -> str | None:
        """What the panel should say about `row`, or None to hide it."""

    def on_data_table_row_highlighted(
        self, event: TextualDataTable.RowHighlighted
    ) -> None:
        self._sync_item_detail_from_key(
            event.row_key.value if event.row_key is not None else None
        )

    def sync_item_detail(self, cursor_row: int) -> None:
        """Resync against whatever the cursor is actually on after a repaint
        -- not left to RowHighlighted alone, since it's not guaranteed to
        re-fire on a table.clear()-and-repopulate while a row is already
        highlighted."""
        # Textual's type, not tcw's: this panel watches whichever table a
        # view points it at, and a plain history table has no reason to be
        # sortable or to carry tooltips.
        table = self.query_one(f"#{self.detail_table_id}", TextualDataTable)  # type: ignore[attr-defined]
        try:
            row_key, _ = table.coordinate_to_cell_key(Coordinate(cursor_row, 0))
        except CellDoesNotExist:
            self._show_item_detail(None)
            return
        self._sync_item_detail_from_key(row_key.value)

    def _sync_item_detail_from_key(self, key: str | None) -> None:
        row = self.detail_rows.get(key) if key is not None else None
        self._show_item_detail(self.detail_text(row) if row else None)

    def _show_item_detail(self, text: str | None) -> None:
        panel = self.query_one(f"#{ITEM_DETAIL}", Static)  # type: ignore[attr-defined]
        if text:
            panel.update(Text(text))
            panel.display = True
        else:
            panel.display = False


class HistoryView(TabbedView, ItemDetailView):
    """A history table plus its `ItemDetailView` panel, and the note above
    the table admitting when it was capped, empty, or errored.

    `PipeView` and `TaskView` both merge two sources into one table --
    everything about that (and about the panel below it) is the same; only
    `detail_text` differs, per `ItemDetailView`.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("d", "show_tab('definition')", "Definition", show=False),
    ]

    detail_table_id = HISTORY_TABLE

    def sync_history_note(
        self,
        repo: Repository,
        dated: Sequence[Any],
        show_days: int,
        noun: str,
        *scopes: Scope,
    ) -> None:
        """Say so when a history was capped rather than exhausted, when it
        settled with nothing in the display window, or when its only fetch
        attempt errored with nothing to fall back on.

        A history table has no natural bottom -- it just stops -- so a
        capped one is indistinguishable from a complete one by looking at
        it, which is exactly the kind of silent partial answer this app is
        not allowed to give. Both halves of the merge are checked: either
        source hitting its cap means the merged table is short of rows, and
        neither one knows about the other's depth.

        Emptiness is judged against `dated` -- this paint's post-window-
        filter merge -- rather than the merge sources' own row counts, since
        the display window (which can be narrower than the fetch window) is
        what actually gates what ends up on screen: older history can exist
        without making today's paint anything but empty.
        """
        note = self.query_one(f"#{HISTORY_NOTE}", Static)  # type: ignore[attr-defined]
        entries = [repo.entry(scope) for scope in scopes]
        capped = any(entry.truncated for entry in entries)
        settled = all(
            entry.state in (CacheState.COMPLETE, CacheState.ERROR) for entry in entries
        )
        errored = next(
            (
                entry
                for entry in entries
                if entry.state is CacheState.ERROR and entry.fetched_at is None
            ),
            None,
        )
        empty = settled and not dated and not capped
        error = empty and errored is not None

        if capped:
            note.update(
                Text(
                    "Showing the newest rows only -- this history reached the "
                    "history row cap, so older rows were not fetched. Raise "
                    "it in Settings, or narrow the fetch window."
                )
            )
        elif error:
            note.update(Text(f"Could not fetch: {errored.error}"))
        elif empty:
            note.update(Text(f"No {noun} in the last {show_days} days."))

        note.set_class(capped, "-capped")
        note.set_class(error, "-error")
        note.display = capped or empty
