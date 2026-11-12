# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Detail views for a schema node (Grants, Tags) and a database node (Grants,
Roles, Tags).

Neither a schema nor a database has an ObjectType (see SchemaRef / docs/DESIGN.md's
"Choosing a hit"), so neither fits the ObjectView/CategoryView registry --
there is exactly one view per node type here, not one of several types
sharing a lookup, so DetailPane constructs SchemaView/DatabaseView directly
rather than through a registry with a single entry.

The two views share tabs, not a base class. A tab both show is a pair of
functions -- the Grants tab's from `grants.py`, which the Table view uses as
well, and the Tags tab's below -- and each view composes the tabs it has and
lists its own scopes. A shared base class would have to grow hooks for
whatever one view has and the other does not, such as the database's Roles.

- **Grants** (`SHOW GRANTS ON SCHEMA`/`SHOW GRANTS ON DATABASE`,
  metadata-layer, no warehouse needed, rendered one row per grantee by
  `grants.py`, the same as the Table view's).
- **Tags** (INFORMATION_SCHEMA.TAG_REFERENCES, an ordinary SELECT that needs a
  running warehouse and so degrades to blank without one -- see
  docs/DESIGN.md's "second source" section).
- **Roles**, the database's only (`SHOW DATABASE ROLES IN DATABASE`,
  metadata-layer like Grants): a schema has no roles of its own.

Tags and Roles are fetched only while their tab is shown, like the Table
view's Grants and Data: each costs a statement per schema or database, and
nothing shows the result until the tab opens.

granted_to (SHOW GRANTS) can be ROLE, DATABASE ROLE, USER, SHARE or
APPLICATION ROLE -- deliberately not filtered down to just roles, since all
of those are legitimate grantees. SHOW GRANTS ON <object> itself only ever
returns what the active role owns, has been granted MANAGE GRANTS on, or
reaches via ACCOUNTADMIN/SECURITYADMIN -- a short or empty Grants table is
expected under a lesser role, not a fetch bug (see docs/DESIGN.md "A second source").
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from tcoda.widgets import DataTable, TabbedContent, TabPane
from textual.app import ComposeResult
from textual.widgets import Static

from nivaria.cache.repository import Repository
from nivaria.format import format_date
from nivaria.journal import Reason
from nivaria.models import (
    SchemaRef,
    Scope,
    database_roles_scope,
    grants_scope,
    tag_references_scope,
)
from nivaria.ui.views.base import _ScopedView
from nivaria.ui.views.grants import compose_grants, paint_grants
from nivaria.ui.views.tables import (
    GRANTS_NOTE,
    ROLES_NOTE,
    TAGS_NOTE,
    HoverTooltipTable,
    PanelledTable,
    sync_scope_note,
)

TAGS_TABLE = "detail-tags"
ROLES_TABLE = "detail-roles"

_TAGS_COLUMNS = ("Tag", "Value")

#: SHOW DATABASE ROLES' own column names, title-cased.
_ROLES_COLUMNS = (
    "Name",
    "Owner",
    "Granted To Roles",
    "Granted To Database Roles",
    "Granted Database Roles",
    "Created",
    "Comment",
)

#: Which row field a shortened cell's full value comes from -- see
#: `HoverTooltipTable`.
_ROLES_TOOLTIPS = {"Created": "created_on"}


class SchemaView(_ScopedView):
    def __init__(self, repo: Repository, ref: SchemaRef) -> None:
        super().__init__(repo)
        self.ref = ref

    def show(self, ref: SchemaRef) -> None:
        """Re-point an already mounted view at another schema."""
        self.ref = ref
        if self.is_mounted:
            self.repaint()

    def export_identity(self) -> tuple[str, str]:
        return "schema", f"{self.ref.database}.{self.ref.name}"

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-grants"):
            with TabPane("Grants", mnemonic="g", id="tab-grants"):
                yield Static("", id=GRANTS_NOTE)
                self.grants_panel = compose_grants()
                yield self.grants_panel
            yield from _compose_tags()

    def grants_scope(self) -> Scope:
        return grants_scope(self.ref.database, self.ref.name)

    def tags_scope(self) -> Scope:
        return tag_references_scope(self.ref.database, self.ref.name)

    def scopes(self) -> list[Scope]:
        scopes = [self.grants_scope()]
        if self._tab_shown("tab-tags"):
            scopes.append(self.tags_scope())
        return scopes

    def table_scopes(self) -> list[Scope]:
        if self._tab_shown("tab-tags"):
            return [self.tags_scope()]
        return [self.grants_scope()]

    def update_content(self) -> None:
        _paint_grants_tab(self, self.grants_panel, self.grants_scope(), "schema")
        if self._tab_shown("tab-tags"):
            _paint_tags(self, self.tags_scope(), "schema")

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.pane.id == "tab-tags":
            self.ensure(self.tags_scope(), Reason.TAB_OPENED)
            _paint_tags(self, self.tags_scope(), "schema")


class DatabaseView(_ScopedView):
    def __init__(self, repo: Repository, database: str) -> None:
        super().__init__(repo)
        self.database = database
        #: This paint's roles, keyed by name, for the Created tooltip.
        self._roles_rows: dict[str, dict[str, Any]] = {}

    def show(self, database: str) -> None:
        """Re-point an already mounted view at another database."""
        self.database = database
        if self.is_mounted:
            self.repaint()

    def export_identity(self) -> tuple[str, str]:
        return "database", self.database

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-grants"):
            with TabPane("Grants", mnemonic="g", id="tab-grants"):
                yield Static("", id=GRANTS_NOTE)
                self.grants_panel = compose_grants()
                yield self.grants_panel
            with TabPane("Roles", mnemonic="r", id="tab-roles"):
                yield Static("", id=ROLES_NOTE)
                yield HoverTooltipTable(self, _ROLES_TOOLTIPS, id=ROLES_TABLE)
            yield from _compose_tags()

    def grants_scope(self) -> Scope:
        return grants_scope(self.database, None)

    def roles_scope(self) -> Scope:
        return database_roles_scope(self.database)

    def tags_scope(self) -> Scope:
        return tag_references_scope(self.database, None)

    def row_for(self, key: str) -> dict[str, Any] | None:
        return self._roles_rows.get(key)

    def scopes(self) -> list[Scope]:
        scopes = [self.grants_scope()]
        if self._tab_shown("tab-roles"):
            scopes.append(self.roles_scope())
        if self._tab_shown("tab-tags"):
            scopes.append(self.tags_scope())
        return scopes

    def table_scopes(self) -> list[Scope]:
        if self._tab_shown("tab-roles"):
            return [self.roles_scope()]
        if self._tab_shown("tab-tags"):
            return [self.tags_scope()]
        return [self.grants_scope()]

    def update_content(self) -> None:
        _paint_grants_tab(self, self.grants_panel, self.grants_scope(), "database")
        if self._tab_shown("tab-roles"):
            self._paint_roles()
        if self._tab_shown("tab-tags"):
            _paint_tags(self, self.tags_scope(), "database")

    def _paint_roles(self) -> None:
        roles = self.roles_scope()
        sync_scope_note(
            self.query_one(f"#{ROLES_NOTE}", Static),
            self.repo.entry(roles),
            empty="No database roles in this database.",
            looking="Looking for database roles...",
        )

        table = self.query_one(f"#{ROLES_TABLE}", DataTable)
        with self.keeping_cursor(table):
            table.clear(columns=True)
            table.add_columns(*_ROLES_COLUMNS)
            self._roles_rows = {}
            for row in self.repo.raw_rows(roles):
                name = row["name"]
                self._roles_rows[name] = row
                table.add_row(
                    Text(name),
                    Text(row.get("owner") or ""),
                    Text(_count(row.get("granted_to_roles"))),
                    Text(_count(row.get("granted_to_database_roles"))),
                    Text(_count(row.get("granted_database_roles"))),
                    Text(format_date(row.get("created_on"))),
                    Text(row.get("comment") or ""),
                    key=name,
                )

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.pane.id == "tab-roles":
            self.ensure(self.roles_scope(), Reason.TAB_OPENED)
            self._paint_roles()
        elif event.pane.id == "tab-tags":
            self.ensure(self.tags_scope(), Reason.TAB_OPENED)
            _paint_tags(self, self.tags_scope(), "database")


def _paint_grants_tab(
    view: _ScopedView, panelled: PanelledTable, scope: Scope, noun: str
) -> None:
    """The Grants table and the note above it, `noun` naming the object as in
    "No grants on this schema"."""
    paint_grants(view, panelled, view.repo.raw_rows(scope))
    sync_scope_note(
        view.query_one(f"#{GRANTS_NOTE}", Static),
        view.repo.entry(scope),
        empty=f"No grants on this {noun} that your role can see.",
        looking="Looking for grants...",
    )


def _compose_tags() -> ComposeResult:
    with TabPane("Tags", mnemonic="t", id="tab-tags"):
        yield Static("", id=TAGS_NOTE)
        yield DataTable(id=TAGS_TABLE, cursor_type="row", zebra_stripes=True)


def _paint_tags(view: _ScopedView, scope: Scope, noun: str) -> None:
    """The Tags table and the note above it. See `_paint_grants_tab`."""
    sync_scope_note(
        view.query_one(f"#{TAGS_NOTE}", Static),
        view.repo.entry(scope),
        empty=f"No tags on this {noun}.",
        looking="Looking for tags...",
    )

    table = view.query_one(f"#{TAGS_TABLE}", DataTable)
    with view.keeping_cursor(table):
        table.clear(columns=True)
        table.add_columns(*_TAGS_COLUMNS)
        for row in view.repo.raw_rows(scope):
            table.add_row(
                Text(row.get("TAG_NAME") or ""),
                Text(row.get("TAG_VALUE") or ""),
            )


def _count(value: int | None) -> str:
    return "" if value is None else str(value)
