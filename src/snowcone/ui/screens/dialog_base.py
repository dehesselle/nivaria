"""Shared chrome for snowcone's centered dialogs.

Subclasses still set their own #dialog width/height and #buttons alignment --
those genuinely differ between a form and a picker -- this only hoists what
every dialog shares.

Escape dismisses with CANCEL_RESULT. A dialog that must not be escapable
overrides `check_action` to disable the binding rather than dropping it:
Textual merges BINDINGS across a class hierarchy, so a subclass cannot remove
one it inherited.
"""

from __future__ import annotations

from typing import Any, ClassVar, TypeVar

from textual.binding import Binding, BindingType
from textual.screen import ModalScreen

ResultT = TypeVar("ResultT")


class DialogScreen(ModalScreen[ResultT]):
    DEFAULT_CSS = """
    DialogScreen {
        align: center middle;
    }
    DialogScreen > #dialog {
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    DialogScreen > #dialog > #title {
        text-style: bold;
        width: 100%;
        content-align: center middle;
    }
    DialogScreen > #dialog > #buttons {
        height: auto;
        align-horizontal: right;
    }
    DialogScreen > #dialog > #buttons Button {
        margin: 0 0 0 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    CANCEL_RESULT: ClassVar[Any] = None

    def action_cancel(self) -> None:
        self.dismiss(self.CANCEL_RESULT)
