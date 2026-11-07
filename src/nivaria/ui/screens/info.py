# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""What this session is connected to, with every name in full.

The main screen's border labels truncate in a narrow pane, and Textual has
tooltips per widget, not per border label, so a hover over one is not on offer.
"""

from __future__ import annotations

from typing import ClassVar

from tcoda.dialogs import Dialog
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.content import Content
from textual.widgets import Button, Label, Static


class InfoDialog(Dialog[None]):
    """Account, role, warehouse and whether the account uses PrivateLink."""

    AUTO_FOCUS: ClassVar[str] = "#info-ok"

    def __init__(
        self, *, account: str, role: str, warehouse: str, privatelink: bool
    ) -> None:
        super().__init__()
        self._rows = (
            ("Account", account),
            ("Role", role),
            ("Warehouse", warehouse),
            ("PrivateLink", "Yes" if privatelink else "No"),
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Info", id="title")
            with Grid(id="info-rows"):
                for name, value in self._rows:
                    yield Label(name)
                    # Content, not a string: a string is parsed as markup, and
                    # a quoted Snowflake name can contain `[`.
                    yield Static(Content(value), classes="info-value")
            with Horizontal(id="buttons"):
                yield Button("Ok", variant="primary", id="info-ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)
