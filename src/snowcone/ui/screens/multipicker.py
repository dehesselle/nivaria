"""A find-as-you-type picker over a fixed list of candidates, for selecting
any number of them rather than exactly one -- used for the database chooser.

Deliberately not sharing code with `PickerDialog` beyond the trivial accent
lookup: the two interaction models diverge everywhere that matters (select-
one-and-dismiss vs. select-any-number-and-confirm-separately), and there
just isn't enough literally-shared logic to justify a base class for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from tcw.dialogs import Dialog
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from snowcone.fuzzy import highlight, rank

#: More than a screenful is wasted ranking; the query narrows it anyway.
MAX_RESULTS = 200

_CHECKED = "☑ "
_UNCHECKED = "☐ "

# Not Textual's own Checkbox: it's a standalone mounted widget, and OptionList
# renders options through a virtualized render_line rather than one child
# widget per row -- using real Checkboxes here would mean mounting/unmounting
# up to MAX_RESULTS of them on every keystroke instead of swapping Text.
# Not SelectionList either, despite it wrapping OptionList the same way this
# does: its selected state lives on the *current* option objects --
# clear_options() wipes self._selected outright, and removing an option
# (_pre_remove_option) auto-deselects it -- so it cannot represent "checked,
# currently filtered out of view", which is exactly what _checked/_shown
# below need every time a query re-filters the list.


class _MultiPickerResults(OptionList):
    """The results list. Its own activation (Enter, or a click -- OptionList
    routes both through `action_select`) toggles a row's checkbox instead of
    picking it; confirming the whole dialog is `MultiPickerDialog.action_choose`,
    reached via the Ok button or `MultiPickerDialog._on_key` intercepting
    Enter before this widget's own binding ever sees it (see there for why a
    second `enter` binding here would not work)."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("space", "select", "Toggle", show=False),
    ]

    def action_select(self) -> None:
        screen = self.screen
        if isinstance(screen, MultiPickerDialog) and self.highlighted is not None:
            screen.toggle(self.highlighted)


class MultiPickerDialog(Dialog[list[str]]):
    """Pick any number of strings from `candidates`, or dismiss with None."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("down", "to_list", "Next", show=False),
    ]

    def __init__(
        self,
        candidates: Sequence[str],
        *,
        title: str = "Pick",
        placeholder: str = "type to filter",
        current: Sequence[str] | None = None,
    ) -> None:
        super().__init__()
        self._candidates = list(candidates)
        self._title = title
        self._placeholder = placeholder
        self._checked: set[str] = set(current or ())
        self._shown: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Input(placeholder=self._placeholder, id="picker-query")
            if self._candidates:
                yield _MultiPickerResults(id="picker-results")
            else:
                yield Static("Nothing to choose from", id="picker-empty")
            with Horizontal(id="buttons"):
                yield Button("All", compact=True, id="multipicker-all")
                yield Button("None", compact=True, id="multipicker-none")
                yield Button("Ok", variant="primary", compact=True, id="multipicker-ok")
                yield Button("Cancel", compact=True, id="multipicker-cancel")

    def on_mount(self) -> None:
        if self._candidates:
            self._refresh("")
        self.query_one("#picker-query", Input).focus()

    # --------------------------------------------------------------- events

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._refresh(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_choose()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        query = self.query_one("#picker-query", Input).value
        match event.button.id:
            case "multipicker-all":
                self._checked |= set(self._shown)
                self._refresh(query, keep_highlight=True)
            case "multipicker-none":
                self._checked -= set(self._shown)
                self._refresh(query, keep_highlight=True)
            case "multipicker-ok":
                self.action_choose()
            case "multipicker-cancel":
                self.action_cancel()

    def _on_key(self, event: events.Key) -> None:
        """Enter always confirms the dialog, even with the results list
        focused. Textual merges a subclass BINDINGS entry for an
        already-bound key as an *additional* candidate tried in base-class-
        first order, not a replacement -- so a second "enter" binding on
        _MultiPickerResults would never win over OptionList's own inherited
        enter-toggles binding. Stealing the key here, before binding dispatch
        runs at all, is the same mechanism ConnectScreen._on_key uses to
        steal escape mid-connect."""
        if event.key == "enter" and isinstance(self.focused, OptionList):
            event.prevent_default()
            event.stop()
            self.action_choose()

    def action_to_list(self) -> None:
        """Arrow down from the query box moves into the results."""
        if self._shown:
            self.query_one("#picker-results", OptionList).focus()

    def action_choose(self) -> None:
        # Candidate order, not filter/click order -- a stable result
        # regardless of what was typed along the way.
        self.dismiss([name for name in self._candidates if name in self._checked])

    def toggle(self, index: int) -> None:
        if not (0 <= index < len(self._shown)):
            return
        name = self._shown[index]
        self._checked.symmetric_difference_update({name})
        self._refresh(self.query_one("#picker-query", Input).value, keep_highlight=True)

    # -------------------------------------------------------------- results

    def _refresh(self, query: str, *, keep_highlight: bool = False) -> None:
        ranked = rank(
            query,
            ((name, name) for name in self._candidates),
            limit=MAX_RESULTS,
        )
        self._shown = [name for name, _ in ranked]
        options = self.query_one("#picker-results", OptionList)
        previous = options.highlighted if keep_highlight else None
        options.clear_options()
        options.add_options(
            [
                Option(self._option_text(name, hit.positions), id=str(index))
                for index, (name, hit) in enumerate(ranked)
            ]
        )
        if self._shown:
            options.highlighted = (
                previous if previous is not None and previous < len(self._shown) else 0
            )

    @property
    def _match_style(self) -> str:
        # Duplicated from PickerDialog._match_style rather than shared: it's
        # an 8-line theme-colour lookup, not domain logic, and not worth a
        # base class when everything else here diverges (see module docstring).
        accent = self.app.theme_variables.get("accent")
        return f"bold {accent}" if accent else "bold"

    def _option_text(self, name: str, positions: tuple[int, ...]) -> Text:
        # Text, not markup: a database name containing brackets would
        # otherwise be swallowed as console markup. The checkbox glyph is
        # prepended before the highlight loop -- it is never part of `name`
        # and never passed to highlight(), so a query can never "match" it.
        style = self._match_style
        text = Text(_CHECKED if name in self._checked else _UNCHECKED)
        for run, matched in highlight(name, positions):
            text.append(run, style=style if matched else "")
        return text
