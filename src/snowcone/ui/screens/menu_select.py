"""The menu bar's bar-only stage.

Highlights one top-level menu at a time and lets the user arrow/mnemonic-pick
which one to open, before any dropdown is shown -- this is what keeps
top-level mnemonics from colliding with item mnemonics once a dropdown is
actually open.
"""

from __future__ import annotations

from typing import ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen

from snowcone.ui.widgets.menubar import MENU_IDS, MENU_LABELS, MenuBar


class MenuBarSelectScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    MenuBarSelectScreen {
        background: transparent;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_none", "Close menu", show=False),
        Binding("left", "move(-1)", "Previous menu", show=False),
        Binding("right", "move(1)", "Next menu", show=False),
        Binding("enter", "open", "Open menu", show=False),
        Binding("down", "open", "Open menu", show=False),
    ]

    def __init__(self, initial_menu_id: str, menu_bar: MenuBar) -> None:
        # Passed in rather than looked up: the menu bar lives on MainScreen,
        # not the app's default screen, so `self.app.query_one` (which only
        # ever searches the default screen) cannot find it once this modal is
        # on top.
        super().__init__()
        self._armed = initial_menu_id
        self._menu_bar = menu_bar

    def on_mount(self) -> None:
        self._sync_armed()

    def _sync_armed(self) -> None:
        self._menu_bar.set_armed(self._armed)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_move(self, direction: int) -> None:
        index = MENU_IDS.index(self._armed)
        self._armed = MENU_IDS[(index + direction) % len(MENU_IDS)]
        self._sync_armed()

    def action_open(self) -> None:
        self.dismiss(self._armed)

    def on_key(self, event: events.Key) -> None:
        character = event.character
        if character is None or not character.isalpha():
            return
        letter = character.lower()
        for menu_id in MENU_IDS:
            if MENU_LABELS[menu_id][0].lower() == letter:
                event.stop()
                self.dismiss(menu_id)
                return
