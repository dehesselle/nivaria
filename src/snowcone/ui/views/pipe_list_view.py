"""Category view for the "Pipes (n)" node: status and load activity.

Tabbed -- Status (one row per pipe: name, execution state, and
`invalid_reason` -- a stopped pipe is otherwise indistinguishable from a
healthy one) opens by default, alongside Load history (last load activity
per pipe). Status needs no new scope: `invalid_reason` is already mirrored
onto the same `SHOW PIPES` rows this view already reads for the Name column.

"Execution State" is the one **inferred** column in this app (see DESIGN.md,
"Column headers mirror Snowflake"): it is not read from anywhere, it is `RUNNING` when `invalid_reason` is empty and `STOPPED`
when it is not. Snowflake's own name for the concept is `SYSTEM$PIPE_STATUS`'
`executionState`, which snowcone never queries -- that is a per-pipe function
call, so showing it for a whole schema would cost one round trip per pipe,
against the round-trips-dominate rule (see DESIGN.md). The column is worth
having anyway: an empty `invalid_reason` cell on its own reads as "nothing
fetched", not "this pipe is fine". `RUNNING`/`STOPPED` borrow
`executionState`'s vocabulary rather than inventing a third one, but only
those two values are reachable -- Snowflake's `PAUSED` and its
`STOPPED_*` variants are not distinguishable from `invalid_reason` alone.

The base rows (every pipe that exists) come from `SHOW PIPES`, already
mirrored for the tree's own category count -- no new fetch scope for those.
Rows/Size/Loaded/Status/Target Table/Filename are this app's second set of
columns sourced from an ordinary `SELECT` instead of `SHOW` (see DESIGN.md's
dual-source note and `table_list_view.py`'s precedent): `SHOW PIPES` carries
no load-activity data at all. That SELECT (against
SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY) needs a running warehouse and elevated
privileges, unlike everything else in this view, so it lives in its own
scope (`pipe_activity_scope`) with its own freshness -- a session missing
either just leaves those columns blank (see `Repository.fail_pass`) while
the SHOW-sourced Name column keeps rendering whatever was cached before.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from snowcone.cache.repository import Repository
from snowcone.format import (
    format_bytes,
    format_timestamp_minutes,
)
from snowcone.models import ObjectRef, ObjectType, Scope, pipe_activity_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.messages import ObjectSelected
from snowcone.ui.views.base import CategoryView, register_category_view
from snowcone.ui.views.tables import (
    LIST_NOTE,
    LIST_TABLE,
    HoverTooltipTable,
    TabbedView,
    sync_list_note,
)

#: The Status tab's own note -- see LIST_NOTE, reused for the Load history
#: tab since both go empty/error together (both read `self.scope` alone).
_STATUS_NOTE = "detail-status-note"

_STATUS_COLUMNS = ("Name", "Execution State", "Invalid Reason")
_HISTORY_COLUMNS = (
    "Name",
    "Rows",
    "Size",
    "Loaded",
    "Status",
    "Target Table",
    "File Name",
)


#: Which merged-row field a tooltip-bearing column's full value comes
#: from, keyed by that column's header. Every other column has no tooltip.
_TOOLTIP_FIELDS = {"Loaded": "LAST_LOAD_TIME"}


@register_category_view
class PipeListView(TabbedView, CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.PIPE

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("s", "show_tab('status')", "Status", show=False),
        Binding("l", "show_tab('history')", "Load history", show=False),
    ]

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by pipe name -- the merged show_pipes +
        #: activity dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-status"):
            with TabPane("[$accent bold]S[/]tatus", id="tab-status"):
                yield Static("", id=_STATUS_NOTE)
                status: DataTable[Text] = DataTable(
                    id="detail-status", cursor_type="row"
                )
                status.zebra_stripes = True
                status.add_columns(*_STATUS_COLUMNS)
                yield status
            with TabPane("[$accent bold]L[/]oad history", id="tab-history"):
                yield Static("", id=LIST_NOTE)
                history: DataTable[Text] = HoverTooltipTable(
                    self, _TOOLTIP_FIELDS, id=LIST_TABLE, cursor_type="row"
                )
                history.zebra_stripes = True
                yield history

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that pipe's detail view, revealed in the tree.

        Reuses the exact path a search hit already takes (`ObjectSelected` ->
        `MainScreen._select` -> `tree.reveal` + `detail.show`), so this needs
        no new message and no MainScreen change.
        """
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectType.PIPE, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectSelected(ref))

    def activity_scope(self) -> Scope:
        return pipe_activity_scope(self.scope.database, self.scope.schema)

    def scopes(self) -> list[Scope]:
        return [self.scope, self.activity_scope()]

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        self.ensure(self.scope, ttl)
        activity_scope = self.activity_scope()
        self.ensure(activity_scope, ttl)
        # Both tabs read self.scope alone, so one Entry drives both notes --
        # activity_scope stays excluded, same as the merge below.
        entry = self.repo.entry(self.scope)
        sync_list_note(self.query_one(f"#{_STATUS_NOTE}", Static), entry, "pipes")
        sync_list_note(self.query_one(f"#{LIST_NOTE}", Static), entry, "pipes")
        # The cache already holds at most one row per pipe -- queries.
        # pipe_activity's QUALIFY keeps only each pipe's latest load. Keyed
        # by PIPE_NAME so a missing/failed activity pass just means an empty
        # dict here -- every row still renders from show_pipes alone.
        activity_by_name: dict[str, dict[str, Any]] = {
            name: row
            for row in self.repo.raw_rows(activity_scope)
            if (name := row.get("PIPE_NAME"))
        }

        status_table = self.query_one("#detail-status", DataTable)
        status_table.clear()
        history_table = self.query_one(f"#{LIST_TABLE}", DataTable)
        history_table.clear(columns=True)
        history_table.add_columns(*_HISTORY_COLUMNS)
        self._rows = {}
        for row in self.repo.raw_rows(self.scope):
            name = row["name"]
            merged = {**row, **activity_by_name.get(name, {})}
            self._rows[name] = merged
            row_count = merged.get("ROW_COUNT")
            history_table.add_row(
                Text(name),
                Text(f"{row_count:,}" if row_count is not None else ""),
                Text(format_bytes(merged.get("FILE_SIZE"))),
                Text(format_timestamp_minutes(merged.get("LAST_LOAD_TIME"))),
                Text(merged.get("STATUS") or ""),
                Text(merged.get("TABLE_NAME") or ""),
                Text(merged.get("FILE_NAME") or ""),
                key=name,
            )
            invalid_reason = row.get("invalid_reason") or ""
            # Derived from invalid_reason, not read from SYSTEM$PIPE_STATUS
            # -- see the module docstring. Upper case because RUNNING/
            # STOPPED are Snowflake's own executionState values, even though
            # only those two of them can ever be reached this way.
            execution_state = "STOPPED" if invalid_reason else "RUNNING"
            status_table.add_row(
                Text(name), Text(execution_state), Text(invalid_reason), key=name
            )
