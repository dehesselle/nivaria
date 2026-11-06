# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Detail view for views: columns, data, grants and SQL.

A view is genuinely both shapes the registry already has: everything a
table shows (`ColumnsView` from `table_view.py` -- Columns, Data and Grants
-- reused whole rather than duplicated), *and* one defining SQL statement,
like a pipe or task (see `pipe_view.py`/`task_view.py`'s SQL tab). So this
is that body plus one tab, mnemonic `s`, after the others.

SQL reads `show_views.text` -- Snowflake's own column for a view's defining
SQL, already mirrored -- straight off `Repository.object_detail`, the same
way `PipeView._update_definition` reads `show_pipes.definition`: no new
scope, just whatever is already cached from the object's own SHOW row.
"""

from __future__ import annotations

from typing import ClassVar

from tcoda.widgets import TabPane
from textual.app import ComposeResult
from textual.widgets import TextArea

from nivaria.models import ObjectType
from nivaria.ui.views.base import register_view
from nivaria.ui.views.table_view import ColumnsView
from nivaria.ui.views.tables import SQL


@register_view
class ViewView(ColumnsView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.VIEW

    def compose_extra_tabs(self) -> ComposeResult:
        with TabPane("SQL", mnemonic="s", id="tab-sql"):
            yield TextArea(
                "",
                id=SQL,
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
        self.query_one(f"#{SQL}", TextArea).text = text
