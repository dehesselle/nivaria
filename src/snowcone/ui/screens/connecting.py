"""A small modal shown while a connection attempt is under way.

Stacked over `ConnectScreen` the same way the pool-growth `ConfirmDialog` is,
rather than a status line woven into the form -- opening a connection is where
every MFA prompt happens, and a dedicated dialog makes that state impossible
to miss (and, unlike a status line, still obviously "waiting" if the terminal
scrolls it out of view).

Every connection this dialog reports on -- the first, up-front one and any
confirmed growth -- can equally involve an MFA prompt, so the two lines shown
here never differ beyond the `(i/N)` count and the seconds remaining: a bold
"Connecting (i/N) ..." and, underneath it, a "(wait for browser / MFA: nn
seconds)" countdown. `N` is always the pool's eventual target size, not
however many this particular `ConnectionPool.connect_all`/`grow` call happens
to be opening, so it reads the same whether it is showing the lone up-front
connection or growth confirmed afterwards.

The countdown starts from `CONNECT_TIMEOUT_SECONDS`, the same value
`ConnectParams.to_connect_kwargs()` passes to the driver as `login_timeout`/
`external_browser_timeout` -- imported from there rather than duplicated, so
the number on screen can never drift from what the driver was actually told.
That bounds a denied or abandoned OAuth or password+MFA approval to a short,
visible wait instead of the driver's undocumented multi-minute default.
**Browser SSO (`externalbrowser`) is the one exception**: the installed
driver's local-socket wait for that authenticator takes no timeout argument
at all (verified by reading it -- see the comment on `CONNECT_TIMEOUT_SECONDS`
in `pool.py`), so an abandoned browser tab under that authenticator can still
leave this dialog showing "0 seconds" indefinitely. Accepted gap, not fixed
by the countdown -- see DESIGN.md.

No Cancel button: the blocking call into `snowflake.connector.connect()`
cannot be interrupted from the outside once made -- the driver exposes no hook
for it -- so a Cancel button could only hide this dialog while the connection
attempt kept running unseen underneath. That is worse than the wait it would
appear to end, so `ConnectScreen` dismisses this dialog itself once the worker
thread actually reports back, and escape is disabled here rather than merely
unbound (see `Dialog`). The countdown above is what keeps that
still-uncancellable wait from being an unbounded, unexplained one instead.
"""

from __future__ import annotations

from tcoda.dialogs import Dialog
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, Static

from snowcone.gateway.pool import CONNECT_TIMEOUT_SECONDS


def _hint(remaining: int) -> str:
    return f"(wait for browser / MFA: {remaining} seconds)"


class ConnectingDialog(Dialog[None]):
    """Shows connection progress. Dismissed by the caller, never by the user."""

    def __init__(self, index: int, total: int) -> None:
        super().__init__()
        self._text = self.message(index, total)
        self._remaining = CONNECT_TIMEOUT_SECONDS

    @staticmethod
    def message(index: int, total: int) -> str:
        return f"Connecting ({index}/{total}) ..."

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._text, id="title")
            yield Static(_hint(self._remaining), id="connecting-hint")

    def on_mount(self) -> None:
        self.set_interval(1, self._tick)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # There is nothing a cancel could actually stop -- see module
        # docstring -- so escape is disabled rather than left to dismiss a
        # dialog the worker thread underneath does not know closed.
        return action != "cancel"

    def update_progress(self, index: int, total: int) -> None:
        # Each connection in a connect_all/grow sequence gets its own fresh
        # countdown window -- `progress` (pool.py) fires before each attempt,
        # so this is the signal that a new one has just started.
        self._remaining = CONNECT_TIMEOUT_SECONDS
        self.query_one("#title", Label).update(self.message(index, total))
        self._paint_hint()

    def _tick(self) -> None:
        self._remaining = max(0, self._remaining - 1)
        self._paint_hint()

    def _paint_hint(self) -> None:
        self.query_one("#connecting-hint", Static).update(_hint(self._remaining))
