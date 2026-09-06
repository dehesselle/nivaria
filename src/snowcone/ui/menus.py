"""Menu contents for the menu bar.

Kept separate from the widgets that render them, mirroring `ui/messages.py`:
one place to look for "what can this menu do" rather than hunting through
widget classes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowcone.ui.screens.menu_dropdown import MenuEntry, MenuItem, MenuSeparator

if TYPE_CHECKING:
    from snowcone.ui.app import SnowconeApp
    from snowcone.ui.screens.main import MainScreen


def connect_menu_items(app: SnowconeApp, screen: MainScreen) -> list[MenuEntry]:
    return [
        MenuItem("open", "Open...", app.open_connect, mnemonic="o"),
        MenuSeparator(),
        MenuItem(
            "database",
            "Database ...",
            screen.pick_database,
            mnemonic="d",
            shortcut="Ctrl+D",
        ),
        MenuItem(
            "warehouse",
            "Warehouse ...",
            screen.pick_warehouse,
            mnemonic="w",
            shortcut="Ctrl+W",
        ),
        MenuItem("role", "Role ...", screen.pick_role, mnemonic="r", shortcut="Ctrl+R"),
        MenuItem("prewarm", "Pre-warm cache", screen.prewarm_cache),
        MenuSeparator(),
        MenuItem("quit", "Quit", app.action_quit, shortcut="F10"),
    ]
