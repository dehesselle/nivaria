"""The object search modal: find-as-you-type over every cached object in the
current database, given the room a modal can take instead of a sliver of the
top pane.

The corpus is built once, when the dialog opens, from whatever is in the cache
right then -- there is no long-lived widget for a background cache update to
mark stale while the dialog is closed, and while it is open `SnowconeApp`
routes `CacheUpdated` to `self.screen`, which by then is this dialog, not
`MainScreen`, so a live update would not reach the tree/detail pane either
(the same is already true of the database picker). Closing and reopening picks
up whatever changed in between, which is the same cost a keystroke used to pay
to notice a stale corpus.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from snowcone.cache.repository import Repository
from snowcone.fuzzy import highlight, rank_qualified
from snowcone.models import ObjectRef
from snowcone.ui.screens.dialog_base import DialogScreen

#: Long enough that holding a key down does not rank once per character,
#: short enough to feel immediate.
DEBOUNCE = 0.12

#: A screenful and then some, even at 90% of a large terminal.
MAX_RESULTS = 200


class SearchScreen(DialogScreen[ObjectRef]):
    """Pick one object by fuzzy name, or dismiss with None."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("down", "to_results", "Results", show=False),
    ]

    def __init__(self, repo: Repository, database: str | None) -> None:
        super().__init__()
        self.repo = repo
        self.database = database
        self._corpus: list[tuple[str, ObjectRef]] = []
        self._shown: list[ObjectRef] = []
        self._timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Input(placeholder="search objects", id="search-query")
            yield OptionList(id="search-results")

    def on_mount(self) -> None:
        self._corpus = self.repo.search_corpus(self.database) if self.database else []
        self.query_one("#search-query", Input).focus()

    # --------------------------------------------------------------- events

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        # Debounced with a named timer: holding a key down would otherwise
        # rank the whole corpus once per character.
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(DEBOUNCE, self._run_query)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        # Enter confirms right away, so it must not act on a stale ranking
        # left over from a debounce that has not fired yet.
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._run_query()
        self._choose(0)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self._choose(int(event.option.id))

    def action_to_results(self) -> None:
        if self._shown:
            self.query_one("#search-results", OptionList).focus()

    # -------------------------------------------------------------- ranking

    def _run_query(self) -> None:
        query = self.query_one("#search-query", Input).value.strip()
        results = self.query_one("#search-results", OptionList)
        if not query:
            self._shown = []
            results.clear_options()
            return

        ranked = rank_qualified(
            query,
            ((ref.schema, ref.name, ref) for _, ref in self._corpus),
            limit=MAX_RESULTS,
        )
        self._shown = [ref for ref, _ in ranked]
        results.clear_options()
        results.add_options(
            [
                Option(self._option_text(ref, hit.positions), id=str(index))
                for index, (ref, hit) in enumerate(ranked)
            ]
        )
        if self._shown:
            results.highlighted = 0

    def _option_text(self, ref: ObjectRef, positions: tuple[int, ...]) -> Text:
        # A Rich Text cannot reference a CSS variable, so the theme's accent
        # has to be resolved to a real colour here.
        accent = self.app.theme_variables.get("accent")
        style = f"bold {accent}" if accent else "bold"
        text = Text()
        for run, matched in highlight(f"{ref.schema}.{ref.name}", positions):
            text.append(run, style=style if matched else "")
        text.append(f"  {ref.kind.value.lower()}", style="dim")
        return text

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self._shown):
            self.dismiss(self._shown[index])
