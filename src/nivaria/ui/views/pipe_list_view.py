# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Category view for the "Pipes (n)" node: status and load activity.

Tabbed -- Status (one row per pipe: name, `created_on`, execution state,
pending files and `invalid_reason`) opens by default, alongside Activity (last
load activity per pipe).

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
from tcoda.widgets import DataTable, TabbedContent, TabPane
from textual.app import ComposeResult
from textual.widgets import Static

from nivaria.format import (
    format_bytes,
    format_date,
    format_timestamp_minutes,
)
from nivaria.journal import Reason
from nivaria.models import (
    CacheState,
    ObjectType,
    Scope,
    pipe_activity_scope,
    pipe_status_scope,
)
from nivaria.snowflake.queries import COPY_HISTORY_PUBLISHED_LAG
from nivaria.ui.views.base import CategoryView, register_category_view
from nivaria.ui.views.tables import (
    LIST_NOTE,
    LIST_TABLE,
    SOURCE_NOTE,
    HoverTooltipTable,
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

_STATUS_COLUMNS = (
    "Name",
    "Created",
    "Execution State",
    "Pending Files",
    "Invalid Reason",
)
_ACTIVITY_COLUMNS = (
    "Name",
    "Rows",
    "Size",
    "Loaded",
    "Status",
    "Target Table",
    "File Name",
)


#: Which merged-row field holds the raw value behind a shortened column,
#: keyed by that column's header, one map per tab. Every other column has no
#: tooltip.
_TOOLTIP_FIELDS = {
    "Rows": "ROW_COUNT",
    "Size": "FILE_SIZE",
    "Loaded": "LAST_LOAD_TIME",
}
_STATUS_TOOLTIP_FIELDS = {
    "Created": "created_on",
    "Pending Files": "pendingFileCount",
}


@register_category_view
class PipeListView(CategoryView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.PIPE

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-status"):
            with TabPane("Activity", mnemonic="a", id="tab-activity"):
                # Every column here but Name comes from ACCOUNT_USAGE.
                yield lag_note("Load activity", COPY_HISTORY_PUBLISHED_LAG)
                yield Static("", id=LIST_NOTE)
                yield Static("", id=SOURCE_NOTE)
                yield HoverTooltipTable(self, _TOOLTIP_FIELDS, id=LIST_TABLE)
            with TabPane("Status", mnemonic="s", id="tab-status"):
                yield Static("", id=_STATUS_NOTE)
                yield Static("", id=_STATUS_SOURCE_NOTE)
                status = HoverTooltipTable(
                    self, _STATUS_TOOLTIP_FIELDS, id="detail-status"
                )
                status.add_columns(*_STATUS_COLUMNS)
                yield status

    def activity_scope(self) -> Scope:
        return pipe_activity_scope(self.scope.database, self.scope.schema)

    def status_scope(self) -> Scope:
        return pipe_status_scope(self.scope.database, self.scope.schema)

    def _status_wanted(self) -> bool:
        """Whether the status scope is on screen, for `scopes`.

        Only while its tab is shown, like the Table view's Grants and Data:
        a pass costs Snowflake time per pipe. And only once the pipe list
        has been fetched, the way `PipeView` waits for a target table: the
        pass names its pipes from that list, and would otherwise complete
        empty and read as fresh.
        """
        return (
            self._tab_shown("tab-status")
            and self.repo.entry(self.scope).fetched_at is not None
        )

    def scopes(self) -> list[Scope]:
        scopes = [self.scope, self.activity_scope()]
        if self._status_wanted():
            scopes.append(self.status_scope())
        return scopes

    def table_scopes(self) -> list[Scope]:
        # Per tab, not `scopes()`: the Activity tab shows nothing of a status
        # pass, which is only fetched while its own tab is up.
        if self._tab_shown("tab-status"):
            return [self.scope, self.status_scope()]
        return [self.scope, self.activity_scope()]

    def update_content(self) -> None:
        activity_scope = self.activity_scope()
        status_scope = self.status_scope()
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
        status_by_name = {
            row["PIPE_NAME"]: _pipe_status(row.get("PIPE_STATUS"))
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
                status = status_by_name.get(name, {})
                merged = {
                    **row,
                    **activity_by_name.get(name, {}),
                    "pendingFileCount": status.get("pendingFileCount"),
                }
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
                    Text(str(status.get("executionState") or "")),
                    Text(_count(status.get("pendingFileCount"))),
                    Text(row.get("invalid_reason") or ""),
                    key=name,
                )

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.pane.id == "tab-status":
            self.repaint(Reason.TAB_OPENED)


def _pipe_status(status: str | None) -> dict[str, Any]:
    """SYSTEM$PIPE_STATUS' JSON as a dict, or empty.

    Its timestamps are deliberately not shown. None says when a pipe last
    took in a file on every kind of pipe: a production pipe that loaded an
    hour earlier reports neither `lastIngestedTimestamp` nor
    `lastForwardedMessageTimestamp`, only times about its queue
    (docs/snowflake-behavior.md "Pipe execution state").
    """
    if not status:
        return {}
    try:
        parsed = json.loads(status)
    except ValueError:
        # A repaint must not fail on whatever else the function may return.
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _count(value: Any) -> str:
    """A count from the JSON, or blank when the function did not say."""
    return f"{value:,}" if isinstance(value, int) else ""
