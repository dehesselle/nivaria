"""Detail view for pipes: load history.

Deliberately a different shape from the column views: a pipe has no columns,
so its detail body is its load history instead -- one row per file it has
loaded, the same dual-source pattern `pipe_list_view.py` uses. The pipe's own
running/stopped state (`invalid_reason`) is compact enough to live in the
context bar (`_status`) rather than the body -- see `compose_context`.

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
from textual.widgets import DataTable, Label, Static

from snowcone.format import format_bytes, format_timestamp_minutes, parse_timestamp
from snowcone.models import ObjectKind, Scope, pipe_activity_scope, pipe_history_scope
from snowcone.settings import CACHE_TTL, PIPE_HISTORY_SHOW_DAYS, config
from snowcone.ui.views.base import ObjectView, register_view

_HISTORY_COLUMNS = ("Rows", "Size", "Loaded", "Status", "Filename")


@register_view
class PipeView(ObjectView):
    OBJECT_KIND: ClassVar[ObjectKind] = ObjectKind.PIPE

    def compose(self) -> ComposeResult:
        table: DataTable[Text] = DataTable(id="pipe-history", cursor_type="row")
        table.zebra_stripes = True
        yield table

    def compose_context(self) -> ComposeResult:
        yield Label(self.ref.fqn, id="context-title")
        yield Static(self._status(), id="context-detail")

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
        for _, r in dated:
            row_count = r.get("ROW_COUNT")
            table.add_row(
                Text(f"{row_count:,}" if row_count is not None else ""),
                Text(format_bytes(r.get("FILE_SIZE"))),
                Text(format_timestamp_minutes(r.get("LAST_LOAD_TIME"))),
                Text(r.get("STATUS") or ""),
                Text(r.get("FILE_NAME") or ""),
            )

    def _status(self) -> str:
        row = self.repo.object_detail(self.ref)
        if row is None:
            return "not cached"
        reason = row.get("invalid_reason")
        return f"stopped: {reason}" if reason else "running"
