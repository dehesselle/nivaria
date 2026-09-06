"""A small modal shown while a connection attempt is under way.

Stacked over `ConnectScreen` the same way the pool-growth `ConfirmScreen` is,
rather than a status line woven into the form -- opening a connection is where
every MFA prompt happens, and a dedicated dialog makes that state impossible
to miss (and, unlike a status line, still obviously "waiting" if the terminal
scrolls it out of view).

Every connection this dialog reports on -- the first, up-front one and any
confirmed growth -- can equally involve an MFA prompt, so the two lines shown
here never differ beyond the `(i/N)` count: a bold "Connecting (i/N) ..." and,
underneath it, the same "(approve any MFA prompt)" hint every time. `N` is
always the pool's eventual target size, not however many this particular
`ConnectionPool.connect_all`/`grow` call happens to be opening, so it reads
the same whether it is showing the lone up-front connection or growth
confirmed afterwards.

No Cancel button: the blocking call into `snowflake.connector.connect()`
cannot be interrupted from the outside once made -- the driver exposes no hook
for it -- so a Cancel button could only hide this dialog while the connection
attempt kept running unseen underneath. That is worse than the wait it would
appear to end, so `ConnectScreen` dismisses this dialog itself once the worker
thread actually reports back, and escape is disabled here rather than merely
unbound (see `DialogScreen`).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, Static

from snowcone.ui.screens.dialog_base import DialogScreen

MFA_HINT = "(approve any MFA prompt)"


class ConnectingScreen(DialogScreen[None]):
    """Shows connection progress. Dismissed by the caller, never by the user."""

    def __init__(self, index: int, total: int) -> None:
        super().__init__()
        self._text = self.message(index, total)

    @staticmethod
    def message(index: int, total: int) -> str:
        return f"Connecting ({index}/{total}) ..."

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._text, id="title")
            yield Static(MFA_HINT, id="connecting-hint")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # There is nothing a cancel could actually stop -- see module
        # docstring -- so escape is disabled rather than left to dismiss a
        # dialog the worker thread underneath does not know closed.
        return action != "cancel"

    def update_progress(self, index: int, total: int) -> None:
        self.query_one("#title", Label).update(self.message(index, total))
