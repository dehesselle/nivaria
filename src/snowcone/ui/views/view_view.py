"""Detail view for views: columns and definition.

A view is the first type that is genuinely both shapes the registry already
has: a real column list, like a table (see `table_view.py`'s `ColumnsView`,
reused here as-is for the Columns tab), *and* one defining SQL statement,
like a pipe or task (see `pipe_view.py`/`task_view.py`'s Definition tab). So
unlike `TableView`, this is tabbed -- Columns and Definition, mnemonic
switchable (`c`/`d`, hidden from the footer), the same pattern
`PipeView`/`TaskView` already establish.

Definition reads `show_views.text` -- Snowflake's own column for a view's
defining SQL, already mirrored -- straight off `Repository.object_detail`,
the same way `PipeView._update_definition` reads `show_pipes.definition`: no
new scope, just whatever is already cached from the object's own SHOW row.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import TabbedContent, TabPane, TextArea

from snowcone.models import ObjectType
from snowcone.ui.views.base import register_view
from snowcone.ui.views.table_view import ColumnsView
from snowcone.ui.views.tables import DEFINITION, TabbedView


@register_view
class ViewView(TabbedView, ColumnsView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.VIEW

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("c", "show_tab('columns')", "Columns", show=False),
        Binding("d", "show_tab('definition')", "Definition", show=False),
    ]

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-columns"):
            with TabPane("[$accent bold]C[/]olumns", id="tab-columns"):
                yield from super().compose()
            with TabPane("[$accent bold]D[/]efinition", id="tab-definition"):
                yield TextArea(
                    "",
                    id=DEFINITION,
                    read_only=True,
                    soft_wrap=True,
                    show_line_numbers=False,
                )

    def update_content(self) -> None:
        super().update_content()
        self._update_definition()

    def _update_definition(self) -> None:
        row = self.repo.object_detail(self.ref)
        text = (row or {}).get("text") or "-- not cached"
        self.query_one(f"#{DEFINITION}", TextArea).text = text
