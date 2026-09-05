from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class MainScreen(Screen):
    """The three-pane browser: tree on the left, context and detail right."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="left"):
            yield Static("DB  (none) ▾", id="db-button")
            yield Static("", id="tree")
        with Vertical(id="right"):
            yield Vertical(id="context")
            yield Vertical(id="detail")
        yield Footer(show_command_palette=False)
