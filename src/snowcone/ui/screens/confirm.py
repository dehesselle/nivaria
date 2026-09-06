"""A generic OK/Cancel question.

Kept free of any particular question's vocabulary -- the connect screen's
"open more connections?" is the first caller, but nothing here is specific to
connections, so the next confirmation need not invent its own dialog.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Static

from snowcone.ui.screens.dialog_base import DialogScreen


class ConfirmScreen(DialogScreen[bool]):
    """Ask a yes/no question. Dismisses `True` for Yes, `False` for No or
    escape."""

    CANCEL_RESULT: ClassVar[bool] = False

    def __init__(self, message: str, *, title: str = "Confirm") -> None:
        super().__init__()
        self._message = message
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Static(self._message, id="confirm-message")
            with Horizontal(id="buttons"):
                yield Button("Yes", variant="primary", id="confirm-yes")
                yield Button("No", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-yes")
