"""A small modal shown while a reconnect is closing the previous session.

`SnowconeApp.open_connect` stops the outgoing `FetchService` and closes the
outgoing `ConnectionPool` off the event-loop thread (`asyncio.to_thread`) so
the UI can actually repaint this dialog instead of hard-freezing, but that
teardown is still a real, unbounded-ish wait -- a worker thread mid-query
cannot be interrupted (see DESIGN.md), so `FetchService.stop()` can block for
a couple of seconds. A dedicated dialog, the same idea as `ConnectingDialog`,
makes that wait visible instead of the app appearing to have hung.

No Cancel button, for the same reason as `ConnectingDialog`: nothing here can
actually be interrupted from the outside once started.
"""

from __future__ import annotations

from tcw.dialogs import Dialog
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label


class ClosingDialog(Dialog[None]):
    """Shown while the previous connection is torn down. Dismissed by the
    caller, never by the user."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Please wait...", id="title")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # See module docstring: there is nothing a cancel could actually stop.
        return action != "cancel"
