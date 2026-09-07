"""A find-as-you-type picker over a fixed list of candidates.

Used for the database chooser now and the global object search later -- the
corpus differs, the interaction does not. Matching runs against an in-memory
list so a keystroke never waits on sqlite, let alone on Snowflake.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from rich.text import Text
from tcw.screens import DialogScreen
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from snowcone.fuzzy import highlight, rank

#: More than a screenful is wasted ranking; the query narrows it anyway.
MAX_RESULTS = 200


class PickerScreen(DialogScreen[str]):
    """Pick one string from `candidates`, or dismiss with None."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("down", "to_list", "Next", show=False),
        Binding("enter", "choose", "Choose", show=False),
    ]

    def __init__(
        self,
        candidates: Sequence[str],
        *,
        title: str = "Pick",
        placeholder: str = "type to filter",
        current: str | None = None,
        hints: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._candidates = list(candidates)
        self._title = title
        self._placeholder = placeholder
        self._current = current
        self._hints = hints or {}
        self._shown: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Input(placeholder=self._placeholder, id="picker-query")
            if self._candidates:
                yield OptionList(id="picker-results")
            else:
                yield Static("Nothing to choose from", id="picker-empty")

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

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.dismiss(self._shown[int(event.option.id)])

    def action_to_list(self) -> None:
        """Arrow down from the query box moves into the results."""
        if self._shown:
            self.query_one("#picker-results", OptionList).focus()

    def action_choose(self) -> None:
        if self._shown:
            options = self.query_one("#picker-results", OptionList)
            index = options.highlighted or 0
            self.dismiss(self._shown[index])

    # -------------------------------------------------------------- results

    def _refresh(self, query: str) -> None:
        ranked = rank(
            query,
            ((name, name) for name in self._candidates),
            limit=MAX_RESULTS,
        )
        self._shown = [name for name, _ in ranked]
        options = self.query_one("#picker-results", OptionList)
        options.clear_options()
        options.add_options(
            [
                Option(self._option_text(name, hit.positions), id=str(index))
                for index, (name, hit) in enumerate(ranked)
            ]
        )
        if self._shown:
            options.highlighted = 0

    @property
    def _match_style(self) -> str:
        """The highlight style, resolved to a real colour.

        A Rich Text cannot reference a CSS variable: handing it "$accent"
        raises MissingStyle at render time. The theme's own value has to be
        looked up and substituted instead.
        """
        accent = self.app.theme_variables.get("accent")
        return f"bold {accent}" if accent else "bold"

    def _option_text(self, name: str, positions: tuple[int, ...]) -> Text:
        """Not _render: that name belongs to Textual's Widget internals, and
        shadowing it breaks rendering of the screen itself."""
        # Text, not markup: an object name containing brackets would otherwise
        # be swallowed as console markup.
        style = self._match_style
        text = Text()
        for run, matched in highlight(name, positions):
            text.append(run, style=style if matched else "")
        annotation = self._annotation(name)
        if annotation:
            text.append(f"  {annotation}", style="dim")
        return text

    def _annotation(self, name: str) -> str | None:
        """Trailing dimmed text for `name`: its hint, its `(current)` marker,
        or both -- one composed mechanic rather than two parallel ones, since
        both are just dimmed context appended after the name."""
        parts = []
        hint = self._hints.get(name)
        if hint:
            parts.append(hint)
        if name == self._current:
            parts.append("(current)")
        return "  ".join(parts) if parts else None
