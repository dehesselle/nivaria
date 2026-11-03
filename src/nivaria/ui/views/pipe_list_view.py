# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Category view for the "Pipes (n)" node: status and load activity.

Tabbed -- Status (one row per pipe: name, `created_on`, execution state
and `invalid_reason`) opens by default, alongside Activity (last load activity
per pipe).

Execution State is Snowflake's own `executionState`, read with
`SYSTEM$PIPE_STATUS` into its own scope (`pipe_status_scope`). Until a pipe's
status has been read, its cell is blank: `invalid_reason` cannot stand in for
it, since a paused pipe, or one whose target table or file format was
dropped, has none (see docs/snowflake-behavior.md "Pipe execution state").
The function takes one pipe at a time, so a pass costs Snowflake time per
pipe; it is asked for only while the Status tab is shown, and only once the
pipe list it names pipes from has been fetched -- see `scopes`.

The base rows (every pipe that exists) come from `SHOW PIPES`, already
mirrored for the tree's own category count -- no new fetch scope for those.
Rows/Size/Loaded/Status/Target Table/Filename are this app's second set of
columns sourced from an ordinary `SELECT` instead of `SHOW` (see docs/DESIGN.md
"A second source" and `table_list_view.py`'s precedent): `SHOW PIPES` carries
no load-activity data at all. That SELECT (against
SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY) needs a running warehouse and elevated
privileges, unlike everything else in this view, so it lives in its own
scope (`pipe_activity_scope`) with its own freshness -- a session missing
either just leaves those columns blank (see `Repository.fail_pass`) while
the SHOW-sourced Name column keeps rendering whatever was cached before.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from nivaria.cache.repository import Repository
from nivaria.format import (
    format_bytes,
    format_date,
    format_timestamp_minutes,
)
from nivaria.gateway.queries import COPY_HISTORY_PUBLISHED_LAG
from nivaria.models import (
    CacheState,
    ObjectRef,
    ObjectType,
    Scope,
    pipe_activity_scope,
    pipe_status_scope,
)
from nivaria.settings import CACHE_TTL, config
from nivaria.ui.messages import ObjectOpened
from nivaria.ui.views.base import CategoryView, register_category_view
from nivaria.ui.views.tables import (
    LIST_NOTE,
    LIST_TABLE,
    SOURCE_NOTE,
    HoverTooltipTable,
    TabbedView,
    lag_note,
    source_line,
    sync_list_note,
    sync_source_note,
)

#: The Status tab's own note -- see LIST_NOTE, reused for the Activity
#: tab since both go empty/error together (both read `self.scope` alone).
_STATUS_NOTE = "detail-status-note"
#: The Status tab's SOURCE_NOTE, for the Execution State column.
_STATUS_SOURCE_NOTE = "detail-status-source-note"

_STATUS_COLUMNS = ("Name", "Created", "Execution State", "Invalid Reason")
_ACTIVITY_COLUMNS = (
    "Name",
    "Rows",
    "Size",
    "Loaded",
    "Status",
    "Target Table",
    "File Name",
)


#: Which merged-row field a tooltip-bearing column's full value comes
#: from, keyed by that column's header, one map per tab. Every other column
#: has no tooltip.
_TOOLTIP_FIELDS = {"Loaded": "LAST_LOAD_TIME"}
_STATUS_TOOLTIP_FIELDS = {"Created": "created_on"}


@register_category_view
class PipeListView(TabbedView, CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.PIPE

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("s", "show_tab('status')", "Status", show=False),
        Binding("a", "show_tab('activity')", "Activity", show=False),
    ]

    def __init__(self, repo: Repository, scope: Scope) -> None:
        super().__init__(repo, scope)
        #: This paint's rows, keyed by pipe name -- the merged show_pipes +
        #: activity dict the hover tooltip reads full-precision values from.
        self._rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-status"):
            with TabPane("[$accent bold]A[/]ctivity", id="tab-activity"):
                # Every column here but Name comes from ACCOUNT_USAGE.
                yield lag_note("Load activity", COPY_HISTORY_PUBLISHED_LAG)
                yield Static("", id=LIST_NOTE)
                yield Static("", id=SOURCE_NOTE)
                activity: DataTable[Text] = HoverTooltipTable(
                    self, _TOOLTIP_FIELDS, id=LIST_TABLE, cursor_type="row"
                )
                activity.zebra_stripes = True
                yield activity
            with TabPane("[$accent bold]S[/]tatus", id="tab-status"):
                yield Static("", id=_STATUS_NOTE)
                yield Static("", id=_STATUS_SOURCE_NOTE)
                status: DataTable[Text] = HoverTooltipTable(
                    self, _STATUS_TOOLTIP_FIELDS, id="detail-status", cursor_type="row"
                )
                status.zebra_stripes = True
                status.add_columns(*_STATUS_COLUMNS)
                yield status

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row opens that pipe's detail view, revealed in the tree.

        Takes the same path a search hit does (`MainScreen._select` ->
        `tree.reveal` + `detail.show`), so the tree cursor follows to the
        object instead of staying on the category, which would leave nothing
        on screen naming what the Detail pane shows. Its own message rather
        than the tree's `ObjectSelected`, which only shows: a tree highlight
        must never reveal (docs/DESIGN.md "A highlight is not a jump"), and
        sharing that message is how this once lost its reveal unnoticed.
        """
        name = event.row_key.value
        if name is None:
            return
        ref = ObjectRef(ObjectType.PIPE, self.scope.database, self.scope.schema, name)
        self.post_message(ObjectOpened(ref))

    def activity_scope(self) -> Scope:
        return pipe_activity_scope(self.scope.database, self.scope.schema)

    def status_scope(self) -> Scope:
        return pipe_status_scope(self.scope.database, self.scope.schema)

    def _status_wanted(self) -> bool:
        """Whether the status scope is on screen, for `scopes` and `ensure`.

        Only while its tab is shown, like the Table view's Grants and Data:
        a pass costs Snowflake time per pipe. And only once the pipe list
        has been fetched, the way `PipeView` waits for a target table: the
        pass names its pipes from that list, and would otherwise complete
        empty and read as fresh.
        """
        return (
            self.is_mounted
            and self.query_one(TabbedContent).active == "tab-status"
            and self.repo.entry(self.scope).fetched_at is not None
        )

    def scopes(self) -> list[Scope]:
        scopes = [self.scope, self.activity_scope()]
        if self._status_wanted():
            scopes.append(self.status_scope())
        return scopes

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._rows.get(key)

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        self.ensure(self.scope, ttl)
        activity_scope = self.activity_scope()
        self.ensure(activity_scope, ttl)
        status_scope = self.status_scope()
        if self._status_wanted():
            self.ensure(status_scope, ttl)
        # Both tabs read self.scope alone, so one Entry drives both notes --
        # activity_scope stays excluded, same as the merge below.
        entry = self.repo.entry(self.scope)
        sync_list_note(self.query_one(f"#{_STATUS_NOTE}", Static), entry, "pipes")
        sync_list_note(self.query_one(f"#{LIST_NOTE}", Static), entry, "pipes")
        sync_source_note(
            self.query_one(f"#{SOURCE_NOTE}", Static),
            [source_line(self.repo.entry(activity_scope), "Load activity")],
        )
        # A status pass reads 50 pipes per statement, at about 25 ms of
        # Snowflake's time each (docs/measurements.md "Pipe status"), so a
        # first pass over a big schema takes a while: say how far it got.
        # Its row count grows with every batch written.
        status_entry = self.repo.entry(status_scope)
        sync_source_note(
            self.query_one(f"#{_STATUS_SOURCE_NOTE}", Static),
            [
                source_line(
                    status_entry,
                    "Execution State",
                    progress=(
                        f"reading, {status_entry.row_count:,} of "
                        f"{entry.row_count:,} pipes"
                        if status_entry.state is CacheState.PARTIAL
                        else None
                    ),
                )
            ],
        )
        # The cache already holds at most one row per pipe -- queries.
        # pipe_activity's QUALIFY keeps only each pipe's latest load. Keyed
        # by PIPE_NAME so a missing/failed activity pass just means an empty
        # dict here -- every row still renders from show_pipes alone.
        activity_by_name: dict[str, dict[str, Any]] = {
            name: row
            for row in self.repo.raw_rows(activity_scope)
            if (name := row.get("PIPE_NAME"))
        }
        # Read whether or not the tab wants a refresh: what an earlier pass
        # stored is still what Snowflake said, within the cache age.
        state_by_name = {
            row["PIPE_NAME"]: _execution_state(row.get("PIPE_STATUS"))
            for row in self.repo.raw_rows(status_scope)
        }

        status_table = self.query_one("#detail-status", DataTable)
        activity_table = self.query_one(f"#{LIST_TABLE}", DataTable)
        with (
            self.keeping_cursor(status_table),
            self.keeping_cursor(activity_table),
        ):
            status_table.clear()
            activity_table.clear(columns=True)
            activity_table.add_columns(*_ACTIVITY_COLUMNS)
            self._rows = {}
            for row in self.repo.raw_rows(self.scope):
                name = row["name"]
                merged = {**row, **activity_by_name.get(name, {})}
                self._rows[name] = merged
                row_count = merged.get("ROW_COUNT")
                activity_table.add_row(
                    Text(name),
                    Text(f"{row_count:,}" if row_count is not None else ""),
                    Text(format_bytes(merged.get("FILE_SIZE"))),
                    Text(format_timestamp_minutes(merged.get("LAST_LOAD_TIME"))),
                    Text(merged.get("STATUS") or ""),
                    Text(merged.get("TABLE_NAME") or ""),
                    Text(merged.get("FILE_NAME") or ""),
                    key=name,
                )
                status_table.add_row(
                    Text(name),
                    Text(format_date(row.get("created_on"))),
                    Text(state_by_name.get(name, "")),
                    Text(row.get("invalid_reason") or ""),
                    key=name,
                )

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.pane.id == "tab-status":
            self.update_content()


def _execution_state(status: str | None) -> str:
    """`executionState` out of SYSTEM$PIPE_STATUS' JSON, or blank."""
    if not status:
        return ""
    try:
        return str(json.loads(status).get("executionState") or "")
    except (ValueError, AttributeError):
        # A repaint must not fail on whatever else the function may return.
        return ""
