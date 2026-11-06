# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Shared building blocks for the detail views.

Three shapes every view here used to write out for itself, and that had each
already drifted:

* **`HoverTooltipTable`** -- a `DataTable` that shows a cell's full-precision
  value on hover, once duplicated verbatim in five list views. Each copy
  carried a `{column index: field}` map, so reordering that view's `_COLUMNS`
  silently pointed its tooltips at the wrong column. This one maps *column
  headers* to fields and resolves the index from the live table, so there is
  no ordering to keep in step.
* **`PanelledTable`** -- a table plus the panel below it that describes
  whichever row is highlighted, hidden when that row has nothing to say.
  Used for `PipeView`/`TaskView`'s load/run errors, a column's tags
  (`ColumnsView`, `table_view.py`) and a grantee's raw grants
  (`grants.py`). A widget rather than the view mixin it used to be: the
  Table view holds two of them (columns and grants), and a mixin had one
  panel per view and answered every table's highlight.
* **`HistoryView`** -- a history table in a `PanelledTable`, plus the
  "capped/empty/errored" note above it.
  `PipeView` and `TaskView` had ~40 identical lines each; only the message
  a row wants to show actually differs, which is the one method a
  subclass supplies.

The widget ids are shared too, and deliberately: exactly one detail view is
mounted at a time (see `DetailPane`), so `#detail-history` means "whichever
history table is on screen". The per-view ids these replaced had already
cost something real: `app.tcss` styled the pipe view's own history table,
detail panel and SQL pane, and the task view -- a later copy of all
three, under its own ids -- silently picked up none of it.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from rich.text import Text
from tcoda.widgets import DataTable
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable as TextualDataTable
from textual.widgets import Static
from textual.widgets.data_table import CellDoesNotExist, ColumnKey, RowKey

from nivaria.cache.repository import Entry, Repository
from nivaria.format import format_timestamp_full
from nivaria.models import CacheState, Scope

#: The one list/table in whichever detail view is mounted.
LIST_TABLE = "detail-list"
#: The line above LIST_TABLE saying so when its scope was fetched and came
#: back empty, or errored with nothing to fall back on -- see
#: `sync_list_note`.
LIST_NOTE = "detail-list-note"
#: A single object's history table, and the panel describing its cursor row.
HISTORY_TABLE = "detail-history"
#: The class of every `PanelledTable`'s panel, which `app.tcss` styles. A
#: class, not a shared id: one view can hold two panels (see `PanelledTable`).
ITEM_DETAIL = "detail-item"
#: The line above a history table admitting that it is not the whole
#: history -- see `HistoryView.sync_history_note`.
HISTORY_NOTE = "detail-history-note"
#: A single object's defining SQL.
SQL = "detail-sql"
#: The line above a list or history saying its rows come from ACCOUNT_USAGE
#: and lag behind -- see `lag_note`.
LAG_NOTE = "detail-lag-note"
#: The line above a list saying which of its columns, those another query
#: fills, are still being fetched or could not be -- see `sync_source_note`.
SOURCE_NOTE = "detail-source-note"
#: The note above a Grants tab, and above a Tags tab -- see `sync_scope_note`.
GRANTS_NOTE = "detail-grants-note"
TAGS_NOTE = "detail-tags-note"


def lag_note(what: str, lag_seconds: int) -> Static:
    """The muted line saying `what`, read from ACCOUNT_USAGE, lags.

    Every refresh asks Snowflake for what is new, but ACCOUNT_USAGE itself
    publishes a row only some time after the event, so the newest load or
    run can be missing for a while whatever the cache age. Stated as
    Snowflake's own "up to" figure, which is not a guarantee, so the
    wording promises no maximum. Muted rather than a warning: it is a
    property of the data, not a failure.
    """
    if lag_seconds % 3600 == 0:
        lag = f"{lag_seconds // 3600} hours"
    else:
        lag = f"{lag_seconds // 60} minutes"
    return Static(Text(f"{what} can be up to {lag} behind."), id=LAG_NOTE)


def failed_refresh(entry: Entry) -> str | None:
    """The note for a refresh that failed while older rows stay on screen.

    `fail_pass` keeps those rows, so without this they look as current as
    any others once the error toast is gone. Stated as the time they were
    fetched rather than their age: the note only changes on a repaint, and
    an age would be wrong by the next minute.
    """
    if entry.state is not CacheState.ERROR or entry.fetched_at is None:
        return None
    fetched = datetime.fromtimestamp(entry.fetched_at, UTC).astimezone()
    return (
        f"Showing rows fetched {fetched:%Y-%m-%d %H:%M}; "
        f"the refresh failed: {entry.error}"
    )


def _answered(entry: Entry) -> bool:
    """Whether a scope has an answer on record, failed or not.

    A refresh keeps the previous rows and their `fetched_at`, so only a
    first fetch counts as unanswered. EMPTY can carry a `fetched_at` with no
    rows behind it -- see `Entry.due`.
    """
    if entry.state is CacheState.ERROR:
        return True
    return entry.fetched_at is not None and entry.state is not CacheState.EMPTY


def sync_scope_note(
    note: Static, entry: Entry, *, empty: str, looking: str | None = None
) -> None:
    """Say why a table showing one scope's rows looks the way it does.

    An empty table reads the same whether the fetch is still out, came back
    with nothing, or failed, so the note says which: `looking` while a first
    fetch is out (None stays silent instead), `empty` once it came back with
    nothing, the error when its only attempt failed, and when the rows shown
    were fetched when a refresh failed. A refresh of rows already on record
    is not waited for, so a revisit stays quiet. Same toggle-display,
    recompute-in-full-every-repaint shape as `HistoryView.sync_history_note`.
    """
    error = entry.state is CacheState.ERROR and entry.fetched_at is None
    stale = failed_refresh(entry)
    is_empty = entry.state is CacheState.COMPLETE and entry.row_count == 0
    is_looking = looking is not None and not _answered(entry)
    if error:
        note.update(Text(f"Could not fetch: {entry.error}"))
    elif stale is not None:
        note.update(Text(stale))
    elif is_empty:
        note.update(Text(empty))
    elif is_looking:
        note.update(Text(looking))
    # Assigned unconditionally, not only in the branches above, so a note
    # widget reused across a view's show() (another schema or object without
    # remounting) never carries a stale class or visibility from the
    # previous paint.
    note.set_class(error or stale is not None, "-error")
    note.display = error or stale is not None or is_empty or is_looking


def sync_list_note(note: Static, entry: Entry, noun: str) -> None:
    """The note for a category's primary scope -- see `sync_scope_note`.
    Silent while it has not been fetched yet."""
    sync_scope_note(note, entry, empty=f"No {noun} in this schema.")


def source_line(
    entry: Entry, label: str, *, progress: str | None = None
) -> tuple[str, bool] | None:
    """What a list's `label` columns cannot show yet, and whether it is a
    failure; None once they show what Snowflake said.

    Those columns come from another query than the list itself (see
    docs/DESIGN.md "A second source"), and a blank cell reads the same
    whether that query is still out, needs a warehouse, or failed. While a
    first fetch is out the line says so, with `progress` if the pass has
    some to report. A refresh of values already on record stays quiet.
    """
    if entry.state is CacheState.ERROR and entry.fetched_at is None:
        return f"{label}: could not fetch: {last_line(entry.error)}", True
    if entry.state is CacheState.ERROR:
        assert entry.fetched_at is not None
        fetched = datetime.fromtimestamp(entry.fetched_at, UTC).astimezone()
        text = (
            f"{label}: as fetched {fetched:%Y-%m-%d %H:%M}; the refresh failed: "
            f"{last_line(entry.error)}"
        )
        return text, True
    if not _answered(entry):
        return f"{label}: {progress or 'looking'}...", False
    return None


def sync_source_note(note: Static, lines: Sequence[tuple[str, bool] | None]) -> None:
    """Show the `source_line`s that have something to say, one per line."""
    said = [line for line in lines if line is not None]
    if said:
        note.update(Text("\n".join(text for text, _ in said)))
    note.set_class(any(failed for _, failed in said), "-error")
    note.display = bool(said)


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


class PanelledTable(Vertical):
    """A table, and below it a panel describing whichever row is highlighted.

    The owning view repaints the table, stores this paint's rows in `rows`
    under the keys it stamped into the table, then calls `sync`. `describe`
    turns one of those rows into the panel's text, or None to hide it.

    Only its own table's highlights are answered. A view with two of these
    (the Table view's columns and grants) would otherwise describe a
    grantee with the column-tags text, looked up under the wrong key.
    """

    def __init__(
        self,
        table: TextualDataTable,
        describe: Callable[[dict[str, Any]], str | None],
        *,
        error: bool = False,
    ) -> None:
        super().__init__(classes="panelled-table")
        self.table = table
        self.describe = describe
        #: This paint's rows, keyed by the row key stamped into the table.
        self.rows: dict[str, dict[str, Any]] = {}
        # Its own id, derived from the table's, so a view with two panels can
        # still name each one.
        self.panel = Static(
            "",
            id=f"{table.id}-item",
            classes=f"{ITEM_DETAIL} -error" if error else ITEM_DETAIL,
        )

    def compose(self) -> ComposeResult:
        yield self.table
        yield self.panel

    def on_data_table_row_highlighted(
        self, event: TextualDataTable.RowHighlighted
    ) -> None:
        if event.data_table is not self.table:
            return
        self._sync_from_key(event.row_key.value if event.row_key is not None else None)

    def sync(self) -> None:
        """Resync against whatever the cursor is actually on after a repaint
        -- not left to RowHighlighted alone, since it's not guaranteed to
        re-fire on a table.clear()-and-repopulate while a row is already
        highlighted."""
        try:
            row_key, _ = self.table.coordinate_to_cell_key(
                Coordinate(self.table.cursor_row, 0)
            )
        except CellDoesNotExist:
            self._show(None)
            return
        self._sync_from_key(row_key.value)

    def _sync_from_key(self, key: str | None) -> None:
        row = self.rows.get(key) if key is not None else None
        self._show(self.describe(row) if row else None)

    def _show(self, text: str | None) -> None:
        if text:
            self.panel.update(Text(text))
            self.panel.display = True
        else:
            self.panel.display = False


class HistoryView:
    """A history table in a `PanelledTable`, and the note above the table
    admitting when it was capped, empty, or errored.

    `PipeView` and `TaskView` both merge two sources into one table --
    everything about that (and about the panel below it) is the same; only
    `detail_text` differs.
    """

    #: Built in `compose_history`, repainted by the subclass.
    history_panel: PanelledTable

    @abstractmethod
    def detail_text(self, row: dict[str, Any]) -> str | None:
        """What the panel should say about a history row, or None to hide
        it."""

    def compose_history(self) -> ComposeResult:
        """The note, the history table and the panel describing its row."""
        yield Static("", id=HISTORY_NOTE)
        history: TextualDataTable[Text] = TextualDataTable(
            id=HISTORY_TABLE, cursor_type="row"
        )
        history.zebra_stripes = True
        self.history_panel = PanelledTable(history, self.detail_text, error=True)
        yield self.history_panel

    def sync_history_note(
        self,
        repo: Repository,
        dated: Sequence[Any],
        show_days: int,
        noun: str,
        sources: Sequence[tuple[Scope, int | None]],
    ) -> None:
        """Say so when a history was capped rather than exhausted, when it
        settled with nothing in the display window, when its only fetch
        attempt errored with nothing to fall back on, when a refresh failed
        and the rows shown are older, or what is known while a first fetch
        is still out.

        A history table has no natural bottom -- it just stops -- so a
        capped one is indistinguishable from a complete one by looking at
        it, which is exactly the kind of silent partial answer this app is
        not allowed to give. Both halves of the merge are checked: either
        source hitting its cap means the merged table is short of rows, and
        neither one knows about the other's depth.

        Each source comes with the days back from now it covers once
        answered, or None for an ACCOUNT_USAGE backfill: its lag means it
        cannot speak for the last hours, so it claims nothing early. The
        near-real-time source answers in a fraction of a second and the
        backfill in seconds (docs/measurements.md "History latency"), and a
        blank table under a busy indicator says nothing while that runs. So
        until every source has answered, the note says what the answered ones
        cover. One that covers the whole display window is the answer.

        A source whose only attempt failed while the other has rows leaves
        part of the history unread, and says which: the near-real-time days,
        or the older backfill. Its rows look complete otherwise -- the
        failure is retried quietly every cache age, so no toast keeps saying
        it.

        Answered means fetched at least once, or failed: a background refresh
        of a cached history keeps its rows and is not waited for, so a
        revisit stays as quiet as before.

        Emptiness is judged against `dated` -- this paint's post-window-
        filter merge -- rather than the merge sources' own row counts, since
        the display window (which can be narrower than the fetch window) is
        what actually gates what ends up on screen: older history can exist
        without making today's paint anything but empty.
        """
        note = self.query_one(f"#{HISTORY_NOTE}", Static)  # type: ignore[attr-defined]
        covers = [(repo.entry(scope), days) for scope, days in sources]
        entries = [entry for entry, _ in covers]
        capped = any(entry.truncated for entry in entries)
        covered = max(
            (
                days
                for entry, days in covers
                if days is not None and _has_rows_on_record(entry)
            ),
            default=0,
        )
        settled = (
            all(
                entry.state is CacheState.ERROR or _has_rows_on_record(entry)
                for entry in entries
            )
            or covered >= show_days
        )
        errored, errored_days = next(
            (
                (entry, days)
                for entry, days in covers
                if entry.state is CacheState.ERROR and entry.fetched_at is None
            ),
            (None, None),
        )
        empty = settled and not dated and not capped
        looking = not settled and not capped
        error = empty and errored is not None
        partial = not capped and not error and errored is not None and bool(dated)
        stale = next(
            (text for entry in entries if (text := failed_refresh(entry))), None
        )

        if capped:
            note.update(
                Text(
                    "Showing newest rows only, raise history row cap in settings "
                    "or narrow fetch window."
                )
            )
        elif error:
            note.update(Text(f"Could not fetch: {errored.error}"))
        elif partial:
            part = (
                f"The last {errored_days} days of {noun}"
                if errored_days is not None
                else f"Older {noun}"
            )
            note.update(Text(f"{part} could not be read: {last_line(errored.error)}"))
        elif stale is not None:
            note.update(Text(stale))
        elif empty:
            note.update(Text(f"No {noun} in the last {show_days} days."))
        elif looking:
            note.update(Text(_looking(dated, noun, covered)))

        failed = not capped and (error or partial or stale is not None)
        note.set_class(capped, "-capped")
        note.set_class(failed, "-error")
        note.display = capped or failed or empty or looking


def _has_rows_on_record(entry: Entry) -> bool:
    """Whether a scope's cached rows are some pass's whole answer.

    A refresh keeps the previous rows and their `fetched_at` while it runs.
    EMPTY can carry a `fetched_at` with no rows behind it -- see `Entry.due`.
    """
    return entry.fetched_at is not None and entry.state is not CacheState.EMPTY


def last_line(message: str | None) -> str:
    """A driver error's last line, which is Snowflake's own wording
    ("Table '...' does not exist or not authorized.") without the error
    number and compilation preamble before it."""
    lines = [line for line in (message or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else (message or "")


def _looking(dated: Sequence[Any], noun: str, covered: int) -> str:
    """What a history note says while some source has not answered yet."""
    if covered and dated:
        return f"Showing the last {covered} days. Still looking for older {noun}..."
    if covered:
        return f"No {noun} in the last {covered} days. Still looking..."
    if dated:
        return f"Still looking for more {noun}..."
    return f"Looking for {noun}..."
