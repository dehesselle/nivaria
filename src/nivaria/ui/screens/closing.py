"""A small modal shown while a session is being closed.

Two callers. `NivariaApp.open_connect` stops the outgoing `FetchService` and
closes the outgoing `ConnectionPool` off the event-loop thread
(`asyncio.to_thread`) so the UI can actually repaint this dialog instead of
hard-freezing, but that teardown is still a real, unbounded-ish wait -- a
worker thread mid-query cannot be interrupted (see DESIGN.md), so
`FetchService.stop()` can block for a couple of seconds. Quitting waits out
the same statements before exiting (`NivariaApp.action_quit`). A dedicated
dialog, the same idea as `ConnectingDialog`, makes either wait visible instead
of the app appearing to have hung.

No Cancel button, for the same reason as `ConnectingDialog`: nothing here can
actually be interrupted from the outside once started. Quitting's way out is
a second Ctrl+Q, which abandons the wait rather than cancelling anything.
"""

from __future__ import annotations

from tcoda.dialogs import Dialog
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, Static


class ClosingDialog(Dialog[None]):
    """Shown while a session is torn down. Dismissed by the caller (or by the
    app exiting), never by the user."""

    def __init__(
        self, message: str = "Please wait...", hint: str | None = None
    ) -> None:
        super().__init__()
        self._message = message
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message, id="title")
            if self._hint is not None:
                yield Static(self._hint, id="closing-hint")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # See module docstring: there is nothing a cancel could actually stop.
        return action != "cancel"
