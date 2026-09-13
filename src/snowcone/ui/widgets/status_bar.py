"""Snowcone's own footer status widgets: pool, warehouse, role.

Plain `Static`s with reactives, mounted into `tcw.widgets.Footer`'s status
area and left alone from then on -- so the screen sets attributes on them
directly. The `StatusFooter` that used to mirror every one of these reactives
and re-establish them with `data_bind` is gone: it only existed because the
stock footer's `recompose()` destroyed these widgets on every focus move.

The countdown moved to `tcw.widgets.Countdown`, which snakecam had grown its
own copy of too.
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static


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

    # layout=True: this is width: auto, and a plain reactive only repaints in
    # place -- a name longer or shorter than whatever was there before would
    # render clipped or leave trailing space until something else (e.g. a
    # terminal resize) happened to force a relayout.
    warehouse: reactive[str | None] = reactive(None, layout=True)

    def render(self) -> str:
        return self.warehouse or "none"


class RoleLabel(Static):
    """The session's current role, or "none" until one is set."""

    role: reactive[str | None] = reactive(None, layout=True)

    def render(self) -> str:
        return self.role or "none"
