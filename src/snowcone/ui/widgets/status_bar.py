"""The footer, carrying key bindings on the left and status on the right.

Subclassing Textual's `Footer` rather than hand-rolling a bar keeps the key
bindings working for free; `show_command_palette=False` frees the right corner,
which Textual otherwise docks ctrl+p into.

Layout note: the command-palette key is the only child `Footer.compose` docks
right. A `1fr` spacer ahead of the indicators -- in normal flow, not docked --
eats the remaining room and pushes them flush right. Docking them right instead
would stack them on top of each other, because Textual takes the *max* width of
same-edge docks rather than the sum.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Footer, Static

#: Braille spinner shown while the focused scope is being refetched.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: The counter is three digits wide; anything longer is a cache age nobody is
#: reading precisely anyway.
MAX_SECONDS = 999


class Countdown(Static):
    """Seconds until the data currently on screen goes stale.

    Not a global timer: it describes the scope behind whatever the cursor is
    on, so it answers "how old is what I am looking at" as you navigate.
    """

    remaining: reactive[int] = reactive(0)
    refreshing: reactive[bool] = reactive(False)
    enabled: reactive[bool] = reactive(True)
    frame: reactive[int] = reactive(0)

    def render(self) -> str:
        if self.refreshing:
            return _SPINNER[self.frame % len(_SPINNER)]
        if not self.enabled:
            return ""
        return str(min(self.remaining, MAX_SECONDS))


class PoolGauge(Static):
    """How many pool connections are in use, e.g. `(2/5)`."""

    in_use: reactive[int] = reactive(0)
    pool_size: reactive[int] = reactive(0)

    def render(self) -> str:
        if self.pool_size == 0:
            return ""
        return f"({self.in_use}/{self.pool_size})"


class WarehouseLabel(Static):
    """The session's current warehouse, or "none" until one is set."""

    warehouse: reactive[str | None] = reactive(None)

    def render(self) -> str:
        return self.warehouse or "none"


class RoleLabel(Static):
    """The session's current role, or "none" until one is set."""

    role: reactive[str | None] = reactive(None)

    def render(self) -> str:
        return self.role or "none"


class StatusFooter(Footer):
    remaining: reactive[int] = reactive(0)
    refreshing: reactive[bool] = reactive(False)
    countdown_enabled: reactive[bool] = reactive(True)
    frame: reactive[int] = reactive(0)
    in_use: reactive[int] = reactive(0)
    pool_size: reactive[int] = reactive(0)
    warehouse: reactive[str | None] = reactive(None)
    role: reactive[str | None] = reactive(None)

    def compose(self) -> ComposeResult:
        yield from super().compose()
        if self._bindings_ready:
            yield Static(classes="footer-spacer")
            yield WarehouseLabel(id="footer-warehouse").data_bind(
                warehouse=StatusFooter.warehouse,
            )
            yield RoleLabel(id="footer-role").data_bind(
                role=StatusFooter.role,
            )
            yield Countdown(id="footer-countdown").data_bind(
                remaining=StatusFooter.remaining,
                refreshing=StatusFooter.refreshing,
                enabled=StatusFooter.countdown_enabled,
                frame=StatusFooter.frame,
            )
            yield PoolGauge(id="footer-pool").data_bind(
                in_use=StatusFooter.in_use,
                pool_size=StatusFooter.pool_size,
            )
