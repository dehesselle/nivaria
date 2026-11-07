# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Nivaria's own footer status widget: the pool gauge.

A plain `Static` with reactives, mounted into `tcoda.widgets.Footer`'s status
area and left alone from then on -- so the screen sets attributes on it
directly. The countdown is `tcoda.widgets.Countdown`.
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
