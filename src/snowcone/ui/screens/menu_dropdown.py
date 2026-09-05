"""A single pulldown menu's contents, positioned under its title."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from rich.console import RenderableType
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from snowcone.ui.widgets.menubar import mnemonic_text


@dataclass(frozen=True)
class MenuItem:
    id: str
    label: str
    handler: Callable[[], Awaitable[None]]
    enabled: bool = True
    shortcut: str | None = None
    mnemonic: str | None = None


def _label_text(item: MenuItem, app: App) -> Text:
    """Render an item's label, coloring its mnemonic letter if it has one."""
    if item.mnemonic is None:
        return Text(item.label)
    index = item.label.lower().find(item.mnemonic.lower())
    return mnemonic_text(item.label, index, app)


def _item_prompt(item: MenuItem, app: App) -> RenderableType:
    """Render an item's label, with its shortcut right-aligned if it has one."""
    label = _label_text(item, app)
    if item.shortcut is None:
        return label
    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")
    grid.add_row(label, Text(item.shortcut, style="dim"))
    return grid


@dataclass(frozen=True)
class OpenNeighbor:
    """Returned by MenuDropdown when Left/Right should switch to the
    adjacent top-level menu instead of selecting an item."""

    direction: int  # -1 or 1


MenuDropdownResult = MenuItem | OpenNeighbor | None


class MenuDropdown(ModalScreen[MenuDropdownResult]):
    """A single pulldown menu's contents, positioned under its title."""

    DEFAULT_CSS = """
    MenuDropdown {
        background: transparent;
    }
    MenuDropdown > OptionList {
        width: 32;
        height: auto;
        max-height: 15;
        border: round $primary;
        background: $surface;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_none", "Close menu", show=False),
        Binding("left", "neighbor(-1)", "Previous menu", show=False),
        Binding("right", "neighbor(1)", "Next menu", show=False),
    ]

    def __init__(self, items: list[MenuItem], *, x_offset: int, menu_id: str) -> None:
        super().__init__()
        self._items = items
        self._x_offset = x_offset
        self._menu_id = menu_id

    def compose(self) -> ComposeResult:
        option_list = OptionList(
            *(
                Option(
                    _item_prompt(item, self.app), id=item.id, disabled=not item.enabled
                )
                for item in self._items
            )
        )
        option_list.styles.offset = (self._x_offset, 1)
        yield option_list

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        item = next((i for i in self._items if i.id == event.option_id), None)
        self.dismiss(item)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_neighbor(self, direction: int) -> None:
        self.dismiss(OpenNeighbor(direction))

    def on_key(self, event: events.Key) -> None:
        character = event.character
        if character is None or not character.isalpha():
            return
        letter = character.lower()
        for item in self._items:
            if (
                item.enabled
                and item.mnemonic is not None
                and item.mnemonic.lower() == letter
            ):
                event.stop()
                self.dismiss(item)
                return
