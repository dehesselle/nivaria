"""The menu bar: a single row of pulldown menu titles plus the version.

Auto-hides by default, Midnight-Commander style -- see `MainScreen._open_menu`
for the two-stage flow (bar-only, then dropdown) that this widget's `armed`
state and `reveal`/`conceal` methods support. Purely presentational plus click
detection: menu contents, mnemonic activation and dropdown behavior all live on
`MainScreen` / `MenuDropdown`.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from snowcone.ui.messages import MenuTitleClicked
from snowcone.version import VERSION

#: Only one menu for now; kept as a list (rather than a single constant) so
#: the staged bar/dropdown flow and its Left/Right neighbor-switching need no
#: special-casing once a second menu is added.
MENU_IDS: list[str] = ["connect"]
MENU_LABELS: dict[str, str] = {"connect": "Connect"}


def mnemonic_text(label: str, index: int, app: App) -> Text:
    """Render `label` as Text with the character at `index` colored in the
    current theme's accent, rather than underlined -- underlines render
    inconsistently across terminals."""
    text = Text(label)
    text.stylize(Style(color=app.theme_variables["accent"]), index, index + 1)
    return text


class _MenuTitle(Static):
    armed: reactive[bool] = reactive(False)

    def __init__(self, menu_id: str) -> None:
        super().__init__(id=f"menu-{menu_id}", classes="menubar--title")
        self.menu_id = menu_id
        self._label = MENU_LABELS[menu_id]

    def on_mount(self) -> None:
        self._refresh_mnemonic_color()
        self.watch(self.app, "theme", self._refresh_mnemonic_color, init=False)

    def _refresh_mnemonic_color(self, *_: object) -> None:
        self.update(mnemonic_text(self._label, 0, self.app))

    def watch_armed(self, armed: bool) -> None:
        self.set_class(armed, "-armed")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(MenuTitleClicked(self.menu_id))


class MenuBar(Widget, can_focus=False):
    """The pulldown menu bar, docked to the top of the main screen."""

    # auto_hide keeps the bar display:none except while a menu is actually in
    # use (MainScreen calls reveal() before opening one, conceal() once it's
    # done), so the object tree and detail pane reclaim its row the rest of
    # the time.
    auto_hide: reactive[bool] = reactive(True)
    revealed: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        for menu_id in MENU_IDS:
            yield _MenuTitle(menu_id)
        yield Static(f"snowcone {VERSION}", id="menubar-version")

    def watch_auto_hide(self) -> None:
        self._sync_display()

    def watch_revealed(self) -> None:
        self._sync_display()

    def _sync_display(self) -> None:
        self.display = not self.auto_hide or self.revealed

    def reveal(self) -> None:
        self.revealed = True

    def conceal(self) -> None:
        if self.auto_hide:
            self.revealed = False

    def set_armed(self, menu_id: str | None) -> None:
        for title in self.query(_MenuTitle):
            title.armed = title.menu_id == menu_id
