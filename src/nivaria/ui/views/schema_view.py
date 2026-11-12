# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Detail view for a schema or database node: Grants and Tags.

Neither a schema nor a database has an ObjectType (see SchemaRef / docs/DESIGN.md's
"Choosing a hit"), so neither fits the ObjectView/CategoryView registry --
there is exactly one view per node type here, not one of several types
sharing a lookup, so DetailPane constructs SchemaView/DatabaseView directly
rather than through a registry with a single entry.

Both share the same two tabs and the same rendering: **Grants** (`SHOW GRANTS
ON SCHEMA`/`SHOW GRANTS ON DATABASE`, metadata-layer, no warehouse needed,
rendered one row per grantee by `grants.py`, the same as the Table view's) and
**Tags** (INFORMATION_SCHEMA.TAG_REFERENCES, an ordinary SELECT that needs a
running warehouse and so degrades to blank without one -- see docs/DESIGN.md's
"second source" section). Tags are fetched only while their tab is shown, like
the Table view's Grants and Data: every cursor stop on a schema would otherwise
use the warehouse, and nothing shows the result until the tab opens. The shared
machinery lives in _GrantsTagsView;
SchemaView and DatabaseView differ only in which scopes they read and how
they're re-pointed to a new object -- not enough difference to justify two
independently maintained tab/table implementations (ColumnsView/TableView/
ViewView is the existing precedent for this split), but a real enough
difference (a database has no schema dimension) that forcing one class with
an optional schema field would trade a shallow abstraction for a confusing
one.

granted_to (SHOW GRANTS) can be ROLE, DATABASE ROLE, USER, SHARE or
APPLICATION ROLE -- deliberately not filtered down to just roles, since all
of those are legitimate grantees. SHOW GRANTS ON <object> itself only ever
returns what the active role owns, has been granted MANAGE GRANTS on, or
reaches via ACCOUNTADMIN/SECURITYADMIN -- a short or empty Grants table is
expected under a lesser role, not a fetch bug (see docs/DESIGN.md "A second source").
"""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from rich.text import Text
from tcoda.widgets import DataTable, TabbedContent, TabPane
from textual.app import ComposeResult
from textual.widgets import Static

from nivaria.cache.repository import Repository
from nivaria.journal import Reason
from nivaria.models import SchemaRef, Scope, grants_scope, tag_references_scope
from nivaria.ui.views.base import _ScopedView
from nivaria.ui.views.grants import compose_grants, paint_grants
from nivaria.ui.views.tables import (
    GRANTS_NOTE,
    TAGS_NOTE,
    sync_scope_note,
)

_TAGS_COLUMNS = ("Tag", "Value")


class _GrantsTagsView(_ScopedView):
    """Shared Grants/Tags tabs and rendering for SchemaView and DatabaseView."""

    #: What the notes call the object, as in "No tags on this schema."
    NOUN: ClassVar[str]

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-grants"):
            with TabPane("Grants", mnemonic="g", id="tab-grants"):
                yield Static("", id=GRANTS_NOTE)
                self.grants_panel = compose_grants()
                yield self.grants_panel
            with TabPane("Tags", mnemonic="t", id="tab-tags"):
                yield Static("", id=TAGS_NOTE)
                tags = DataTable(id="detail-tags", cursor_type="row")
                tags.zebra_stripes = True
                yield tags

    @abstractmethod
    def grants_scope(self) -> Scope: ...

    @abstractmethod
    def tags_scope(self) -> Scope: ...

    def scopes(self) -> list[Scope]:
        scopes = [self.grants_scope()]
        if self._tab_shown("tab-tags"):
            scopes.append(self.tags_scope())
        return scopes

    def table_scopes(self) -> list[Scope]:
        return [
            self.tags_scope() if self._tab_shown("tab-tags") else self.grants_scope()
        ]

    def update_content(self) -> None:
        grants = self.grants_scope()
        paint_grants(self, self.grants_panel, self.repo.raw_rows(grants))
        sync_scope_note(
            self.query_one(f"#{GRANTS_NOTE}", Static),
            self.repo.entry(grants),
            empty=f"No grants on this {self.NOUN} that your role can see.",
            looking="Looking for grants...",
        )
        if self._tab_shown("tab-tags"):
            self._update_tags()

    def _update_tags(self) -> None:
        tags = self.tags_scope()
        sync_scope_note(
            self.query_one(f"#{TAGS_NOTE}", Static),
            self.repo.entry(tags),
            empty=f"No tags on this {self.NOUN}.",
            looking="Looking for tags...",
        )

        tags_table = self.query_one("#detail-tags", DataTable)
        with self.keeping_cursor(tags_table):
            tags_table.clear(columns=True)
            tags_table.add_columns(*_TAGS_COLUMNS)
            for row in self.repo.raw_rows(tags):
                tags_table.add_row(
                    Text(row.get("TAG_NAME") or ""),
                    Text(row.get("TAG_VALUE") or ""),
                )

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.pane.id == "tab-tags":
            self.ensure(self.tags_scope(), Reason.TAB_OPENED)
            self._update_tags()


class SchemaView(_GrantsTagsView):
    NOUN = "schema"

    def __init__(self, repo: Repository, ref: SchemaRef) -> None:
        super().__init__(repo)
        self.ref = ref

    def show(self, ref: SchemaRef) -> None:
        """Re-point an already mounted view at another schema."""
        self.ref = ref
        if self.is_mounted:
            self.repaint()

    def export_identity(self) -> tuple[str, str]:
        return self.NOUN, f"{self.ref.database}.{self.ref.name}"

    def grants_scope(self) -> Scope:
        return grants_scope(self.ref.database, self.ref.name)

    def tags_scope(self) -> Scope:
        return tag_references_scope(self.ref.database, self.ref.name)


class DatabaseView(_GrantsTagsView):
    NOUN = "database"

    def __init__(self, repo: Repository, database: str) -> None:
        super().__init__(repo)
        self.database = database

    def show(self, database: str) -> None:
        """Re-point an already mounted view at another database."""
        self.database = database
        if self.is_mounted:
            self.repaint()

    def export_identity(self) -> tuple[str, str]:
        return self.NOUN, self.database

    def grants_scope(self) -> Scope:
        return grants_scope(self.database, None)

    def tags_scope(self) -> Scope:
        return tag_references_scope(self.database, None)
