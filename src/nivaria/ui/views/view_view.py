# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Detail view for views: columns, data, grants and SQL.

A view is genuinely both shapes the registry already has: everything a
table shows (`ColumnsView` from `table_view.py` -- Columns, Data and Grants
-- reused whole rather than duplicated), *and* one defining SQL statement,
like a pipe or task. So this is that body plus one tab, mnemonic `s`, after
the others.

SQL reads `show_views.text` -- Snowflake's own column for a view's defining
SQL, already mirrored -- the same way a pipe's reads `show_pipes.definition`:
no new scope, just whatever is already cached from the object's own SHOW row.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult

from nivaria.models import ObjectType
from nivaria.ui.views.base import register_view
from nivaria.ui.views.table_view import ColumnsView


@register_view
class ViewView(ColumnsView):
    OBJECT_TYPE: ClassVar[ObjectType] = ObjectType.VIEW

    def compose_extra_tabs(self) -> ComposeResult:
        yield from self.compose_sql_tab()

    def update_content(self) -> None:
        super().update_content()
        self.paint_sql("text")
