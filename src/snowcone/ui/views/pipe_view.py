"""Detail view for pipes: load history and definition.

Deliberately a different shape from the column views: a pipe has no columns,
so its body is tabbed instead -- Load history (one row per file it has
loaded, the same dual-source pattern `pipe_list_view.py` uses, plus a detail
panel below for whatever the highlighted row wants to say about itself --
currently its error, if any) and Definition (the pipe's raw COPY INTO SQL,
unformatted).

Status (pipe/status/invalid_reason) lives in `pipe_list_view.py` instead of
here -- a single pipe's view can only ever show one row of it, which can
never answer the question a status tab is for ("which of my pipes are
broken?"). The category view already lists every pipe in the schema, so it
can show status for all of them at once.

The history merges two sources: `pipe_activity_scope` (SNOWFLAKE.
ACCOUNT_USAGE.COPY_HISTORY, the full backfill within the "pipe history fetch
window" setting) and `pipe_history_scope` (INFORMATION_SCHEMA.COPY_HISTORY,
near-real-time but limited to Snowflake's own 14-day retention for that
function). Rows are merged by (FILE_NAME, LAST_LOAD_TIME), with the
near-real-time source inserted last so it wins on overlap -- robust to either
source being empty or failed, since there is no need to reason about which
scope succeeded. The result is filtered to the "pipe history display window"
setting, which can be shorter than the fetch window: shrinking it later is
a display-only change, since the wider range stays cached either way.

`pipe_history_scope` needs the pipe's target table, which Snowflake exposes
nowhere except by resolving it from whatever `pipe_activity_scope` already
has cached (`Repository.pipe_target_table`) -- so a pipe with no cached
activity at all (never loaded anything, or the schema's activity scope has
not been fetched yet) has no target to give it. That is a normal, permanent
state for plenty of pipes, not a transient failure: this view does not
`ensure()` -- or even list in `scopes()` -- the history scope until a target
is actually resolvable, so such a pipe just shows an empty table rather than
repeatedly failing a fetch it can never succeed at.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static, TabbedContent, TabPane, TextArea
from textual.widgets.data_table import CellDoesNotExist

from snowcone.cache.repository import Repository
from snowcone.format import format_bytes, format_timestamp_minutes, parse_timestamp
from snowcone.models import (
    ObjectKind,
    ObjectRef,
    Scope,
    pipe_activity_scope,
    pipe_history_scope,
)
from snowcone.settings import CACHE_TTL, PIPE_HISTORY_SHOW_DAYS, config
from snowcone.ui.views.base import ObjectView, register_view

_HISTORY_COLUMNS = ("Rows", "Size", "Loaded", "Status", "Filename")


@register_view
class PipeView(ObjectView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.PIPE

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("l", "show_tab('history')", "Load history", show=False),
        Binding("d", "show_tab('definition')", "Definition", show=False),
    ]

    def __init__(self, repo: Repository, ref: ObjectRef) -> None:
        super().__init__(repo, ref)
        #: This paint's history rows, keyed by the row key stamped in
        #: update_content -- looked back up when a row is highlighted.
        self._history_rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-history"):
            with TabPane("[$accent bold]L[/]oad history", id="tab-history"):
                history: DataTable[Text] = DataTable(
                    id="pipe-history", cursor_type="row"
                )
                history.zebra_stripes = True
                yield history
                yield Static("", id="pipe-history-detail")
            with TabPane("[$accent bold]D[/]efinition", id="tab-definition"):
                yield TextArea(
                    "",
                    id="pipe-definition",
                    read_only=True,
                    soft_wrap=True,
                    show_line_numbers=False,
                )

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = f"tab-{tab}"

    def activity_scope(self) -> Scope:
        return pipe_activity_scope(self.ref.database, self.ref.schema)

    def history_scope(self) -> Scope:
        return pipe_history_scope(self.ref.database, self.ref.schema, self.ref.name)

    def target_table(self) -> tuple[str, str, str] | None:
        return self.repo.pipe_target_table(
            self.ref.database, self.ref.schema, self.ref.name
        )

    def scopes(self) -> list[Scope]:
        # history_scope is only meaningfully "on screen" once a target table
        # is resolvable -- see the module docstring. Omitting it otherwise
        # keeps both the interactive ensure() below and the background sweep
        # (MainScreen._sweep, which watches self.detail.scopes()) from ever
        # requesting a fetch that cannot succeed.
        scopes = [self.activity_scope()]
        if self.target_table() is not None:
            scopes.append(self.history_scope())
        return scopes

    def update_content(self) -> None:
        self._update_history()
        self._update_definition()

    def _update_history(self) -> None:
        ttl = config.get(CACHE_TTL)
        activity_scope = self.activity_scope()
        self.ensure(activity_scope, ttl)

        target = self.target_table()
        history_scope = self.history_scope() if target is not None else None
        if history_scope is not None:
            self.ensure(history_scope, ttl)

        # Merged by (FILE_NAME, LAST_LOAD_TIME) -- see the module docstring
        # for why this needs no "which scope succeeded" branching.
        merged: dict[tuple[Any, Any], dict[str, Any]] = {}
        for r in self.repo.raw_rows(activity_scope):
            if r.get("PIPE_NAME") != self.ref.name:
                continue
            merged[(r.get("FILE_NAME"), r.get("LAST_LOAD_TIME"))] = r
        if history_scope is not None:
            for r in self.repo.raw_rows(history_scope):
                merged[(r.get("FILE_NAME"), r.get("LAST_LOAD_TIME"))] = r

        show_days = config.get(PIPE_HISTORY_SHOW_DAYS)
        cutoff = datetime.now().astimezone() - timedelta(days=show_days)
        dated: list[tuple[datetime, dict[str, Any]]] = []
        for r in merged.values():
            loaded_at = parse_timestamp(r.get("LAST_LOAD_TIME"))
            if loaded_at is not None and loaded_at >= cutoff:
                dated.append((loaded_at, r))
        dated.sort(key=lambda pair: pair[0], reverse=True)

        table = self.query_one("#pipe-history", DataTable)
        table.clear(columns=True)
        table.add_columns(*_HISTORY_COLUMNS)
        self._history_rows = {}
        for index, (_, r) in enumerate(dated):
            key = str(index)
            self._history_rows[key] = r
            row_count = r.get("ROW_COUNT")
            table.add_row(
                Text(f"{row_count:,}" if row_count is not None else ""),
                Text(format_bytes(r.get("FILE_SIZE"))),
                Text(format_timestamp_minutes(r.get("LAST_LOAD_TIME"))),
                Text(r.get("STATUS") or ""),
                Text(r.get("FILE_NAME") or ""),
                key=key,
            )
        self._sync_history_detail(table.cursor_row)

    def _update_definition(self) -> None:
        row = self.repo.object_detail(self.ref)
        text = (row or {}).get("definition") or "-- not cached"
        self.query_one("#pipe-definition", TextArea).text = text

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._sync_history_detail_from_key(
            event.row_key.value if event.row_key is not None else None
        )

    def _sync_history_detail(self, cursor_row: int) -> None:
        """Resync against whatever the cursor is actually on after a repaint
        -- not left to RowHighlighted alone, since it's not guaranteed to
        re-fire on a table.clear()-and-repopulate while a row is already
        highlighted."""
        table = self.query_one("#pipe-history", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(Coordinate(cursor_row, 0))
        except CellDoesNotExist:
            self._show_history_detail(None)
            return
        self._sync_history_detail_from_key(row_key.value)

    def _sync_history_detail_from_key(self, key: str | None) -> None:
        row = self._history_rows.get(key) if key is not None else None
        message = row.get("FIRST_ERROR_MESSAGE") if row else None
        if not message:
            self._show_history_detail(None)
            return
        line = row.get("FIRST_ERROR_LINE_NUMBER")
        text = f"Line {line}: {message}" if line is not None else message
        self._show_history_detail(text)

    def _show_history_detail(self, text: str | None) -> None:
        panel = self.query_one("#pipe-history-detail", Static)
        if text:
            panel.update(Text(text))
            panel.display = True
        else:
            panel.display = False
