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
* **`HistoryView`** -- a two-source history table plus the panel underneath
  that describes the highlighted row. `PipeView` and `TaskView` had ~40
  identical lines each; only the message a row wants to show actually
  differs, which is the one method a subclass supplies.

The widget ids are shared too, and deliberately: exactly one detail view is
mounted at a time (see `DetailPane`), so `#detail-history` means "whichever
history table is on screen". The per-view ids these replaced had already
cost something real: `app.tcss` styled the pipe view's own history table,
error panel and definition pane, and the task view -- a later copy of all
three, under its own ids -- silently picked up none of it.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static, TabbedContent
from textual.widgets.data_table import CellDoesNotExist

from snowcone.cache.repository import Repository
from snowcone.format import format_timestamp_full
from snowcone.models import Scope

#: The one list/table in whichever detail view is mounted.
LIST_TABLE = "detail-list"
#: A single object's history table, and the panel describing its cursor row.
HISTORY_TABLE = "detail-history"
HISTORY_DETAIL = "detail-history-error"
#: The line above a history table admitting that it is not the whole
#: history -- see `HistoryView.sync_history_note`.
HISTORY_NOTE = "detail-history-note"
#: A single object's defining SQL.
DEFINITION = "detail-definition"


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

    `tooltips` maps a column *header* to the merged-row field holding its
    untruncated value. Keying on the header rather than the column index is
    what lets a view reorder its columns without silently mis-aiming its own
    tooltips.
    """

    def __init__(
        self, view: RowSource, tooltips: dict[str, str], **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._view = view
        self._tooltips = tooltips

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        # Textual dispatches either the underscored or the bare form per
        # class, not both, and DataTable's own tooltip handling needs this
        # one -- see watch_hover_coordinate below.
        super()._on_mouse_move(event)
        # DataTable leaves hover_coordinate untouched when the mouse moves
        # over filler space that carries no row/column meta, so a tooltip set
        # while over a real cell would otherwise never get the reactive
        # change needed to clear it here.
        meta = event.style.meta
        if not meta or meta.get("out_of_bounds", False):
            self.tooltip = None

    def _field_at(self, column: int) -> str | None:
        columns = self.ordered_columns
        if not 0 <= column < len(columns):
            return None
        return self._tooltips.get(columns[column].label.plain)

    def watch_hover_coordinate(self, old: Coordinate, value: Coordinate) -> None:
        super().watch_hover_coordinate(old, value)
        field = self._field_at(value.column)
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


class TabbedView:
    """Mnemonic tab switching, for the views whose body is a `TabbedContent`.

    A mixin rather than a base class: the views that need it already inherit
    from `ObjectView`, `CategoryView` or `_ScopedView`, and what they share is
    only this one action.
    """

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = f"tab-{tab}"  # type: ignore[attr-defined]


class HistoryView(TabbedView):
    """A history table plus a panel describing whichever row is highlighted.

    `PipeView` and `TaskView` both merge two sources into one table and then
    want to say something about the cursor row that does not fit in a cell --
    a load error, a run error. Everything about that is the same; only
    `detail_text` differs.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("d", "show_tab('definition')", "Definition", show=False),
    ]

    #: This paint's history rows, keyed by the row key stamped into the table
    #: -- looked back up when a row is highlighted. Set by the subclass on
    #: every repaint.
    history_rows: dict[str, dict[str, Any]]

    def sync_history_note(self, repo: Repository, *scopes: Scope) -> None:
        """Say so when a history was capped rather than exhausted.

        A history table has no natural bottom -- it just stops -- so a
        capped one is indistinguishable from a complete one by looking at
        it, which is exactly the kind of silent partial answer this app is
        not allowed to give. Both halves of the merge are checked: either
        source hitting its cap means the merged table is short of rows,
        and neither one knows about the other's depth.
        """
        note = self.query_one(f"#{HISTORY_NOTE}", Static)  # type: ignore[attr-defined]
        capped = any(repo.entry(scope).truncated for scope in scopes)
        if capped:
            note.update(
                Text(
                    "Showing the newest rows only -- this history reached the "
                    "history row cap, so older rows were not fetched. Raise "
                    "it in Settings, or narrow the fetch window."
                )
            )
        note.display = capped

    @abstractmethod
    def detail_text(self, row: dict[str, Any]) -> str | None:
        """What the panel should say about `row`, or None to hide it."""

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._sync_history_detail_from_key(
            event.row_key.value if event.row_key is not None else None
        )

    def sync_history_detail(self, cursor_row: int) -> None:
        """Resync against whatever the cursor is actually on after a repaint
        -- not left to RowHighlighted alone, since it's not guaranteed to
        re-fire on a table.clear()-and-repopulate while a row is already
        highlighted."""
        table = self.query_one(f"#{HISTORY_TABLE}", DataTable)  # type: ignore[attr-defined]
        try:
            row_key, _ = table.coordinate_to_cell_key(Coordinate(cursor_row, 0))
        except CellDoesNotExist:
            self._show_history_detail(None)
            return
        self._sync_history_detail_from_key(row_key.value)

    def _sync_history_detail_from_key(self, key: str | None) -> None:
        row = self.history_rows.get(key) if key is not None else None
        self._show_history_detail(self.detail_text(row) if row else None)

    def _show_history_detail(self, text: str | None) -> None:
        panel = self.query_one(f"#{HISTORY_DETAIL}", Static)  # type: ignore[attr-defined]
        if text:
            panel.update(Text(text))
            panel.display = True
        else:
            panel.display = False
