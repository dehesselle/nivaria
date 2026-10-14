"""Detail view for a schema or database node: Grants and Tags.

Neither a schema nor a database has an ObjectType (see SchemaRef / DESIGN.md's
"Choosing a hit"), so neither fits the ObjectView/CategoryView registry --
there is exactly one view per node type here, not one of several types
sharing a lookup, so DetailPane constructs SchemaView/DatabaseView directly
rather than through a registry with a single entry.

Both share the same two tabs and the same rendering: **Grants** (`SHOW GRANTS
ON SCHEMA`/`SHOW GRANTS ON DATABASE`, metadata-layer, no warehouse needed,
rendered one row per grantee by `grants.py`, the same as the Table view's) and
**Tags** (INFORMATION_SCHEMA.TAG_REFERENCES, an ordinary SELECT that needs a
running warehouse and so degrades to blank without one, the same dual-source
pattern table_stats/information_schema_tables established -- see DESIGN.md's
"second source" section). The shared machinery lives in _GrantsTagsView;
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
expected under a lesser role, not a fetch bug (see DESIGN.md "Known gaps").
"""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, TabbedContent, TabPane

from snowcone.cache.repository import Repository
from snowcone.models import SchemaRef, Scope, grants_scope, tag_references_scope
from snowcone.settings import CACHE_TTL, config
from snowcone.ui.views.base import _ScopedView
from snowcone.ui.views.grants import compose_grants, paint_grants
from snowcone.ui.views.tables import TabbedView

_TAGS_COLUMNS = ("Tag", "Value")


class _GrantsTagsView(TabbedView, _ScopedView):
    """Shared Grants/Tags tabs and rendering for SchemaView and DatabaseView."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("g", "show_tab('grants')", "Grants", show=False),
        Binding("t", "show_tab('tags')", "Tags", show=False),
    ]

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-grants"):
            with TabPane("[$accent bold]G[/]rants", id="tab-grants"):
                self.grants_panel = compose_grants()
                yield self.grants_panel
            with TabPane("[$accent bold]T[/]ags", id="tab-tags"):
                tags: DataTable[Text] = DataTable(id="detail-tags", cursor_type="row")
                tags.zebra_stripes = True
                yield tags

    def on_mount(self) -> None:
        self.update_content()

    @abstractmethod
    def grants_scope(self) -> Scope: ...

    @abstractmethod
    def tags_scope(self) -> Scope: ...

    def scopes(self) -> list[Scope]:
        return [self.grants_scope(), self.tags_scope()]

    def update_content(self) -> None:
        ttl = config.get(CACHE_TTL)
        grants = self.grants_scope()
        tags = self.tags_scope()
        self.ensure(grants, ttl)
        self.ensure(tags, ttl)

        paint_grants(self, self.grants_panel, self.repo.raw_rows(grants))

        tags_table = self.query_one("#detail-tags", DataTable)
        with self.keeping_cursor(tags_table):
            tags_table.clear(columns=True)
            tags_table.add_columns(*_TAGS_COLUMNS)
            for row in self.repo.raw_rows(tags):
                tags_table.add_row(
                    Text(row.get("TAG_NAME") or ""),
                    Text(row.get("TAG_VALUE") or ""),
                )


class SchemaView(_GrantsTagsView):
    def __init__(self, repo: Repository, ref: SchemaRef) -> None:
        super().__init__()
        self.repo = repo
        self.ref = ref

    def show(self, ref: SchemaRef) -> None:
        """Re-point an already mounted view at another schema."""
        self.ref = ref
        if self.is_mounted:
            self.update_content()

    def grants_scope(self) -> Scope:
        return grants_scope(self.ref.database, self.ref.name)

    def tags_scope(self) -> Scope:
        return tag_references_scope(self.ref.database, self.ref.name)


class DatabaseView(_GrantsTagsView):
    def __init__(self, repo: Repository, database: str) -> None:
        super().__init__()
        self.repo = repo
        self.database = database

    def show(self, database: str) -> None:
        """Re-point an already mounted view at another database."""
        self.database = database
        if self.is_mounted:
            self.update_content()

    def grants_scope(self) -> Scope:
        return grants_scope(self.database, None)

    def tags_scope(self) -> Scope:
        return tag_references_scope(self.database, None)
