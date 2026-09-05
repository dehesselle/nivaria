"""Find-as-you-type search over every cached object in the current database.

Answered entirely from the cache, like everything else the UI reads: typing
never waits on Snowflake, and never waits on sqlite either, because the corpus
is held in memory between keystrokes. Ranking runs on the event loop, which
holds for the corpus sizes this is built for -- see fuzzy.py for the measured
cost and the size at which that stops being true.

The corpus is rebuilt lazily. A cache update marks it stale rather than
rebuilding immediately -- a database-wide prime lands dozens of scope updates
in a second, and rebuilding on each would be wasted work nobody is looking at.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from snowcone.cache.repository import Repository
from snowcone.fuzzy import highlight, rank_qualified
from snowcone.models import ObjectRef
from snowcone.ui.messages import ObjectSelected

#: Long enough that holding a key down does not rank once per character,
#: short enough to feel immediate.
DEBOUNCE = 0.12

#: More than a screenful of results is ranking nobody reads.
MAX_RESULTS = 100


class SearchBar(Vertical):
    """A query box plus its ranked results."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "clear", "Clear", show=False),
        Binding("down", "to_results", "Results", show=False),
    ]

    class Activity(Message):
        """Search became active or inactive.

        The screen uses this to swap the context bar out of the way, because
        the top pane shows one thing at a time: what you are looking at, or
        what you are looking for.
        """

        def __init__(self, active: bool) -> None:
            self.active = active
            super().__init__()

    def __init__(self, repo: Repository, **kwargs) -> None:
        super().__init__(**kwargs)
        self.repo = repo
        self.database: str | None = None
        self._corpus: list[tuple[str, ObjectRef]] = []
        self._stale = True
        self._shown: list[ObjectRef] = []
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search objects", id="search-query")
        yield OptionList(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-results", OptionList).display = False

    # ---------------------------------------------------------------- state

    @property
    def active(self) -> bool:
        return bool(self._shown) or bool(self._query)

    @property
    def _query(self) -> str:
        return self.query_one("#search-query", Input).value.strip()

    def set_database(self, database: str | None) -> None:
        self.database = database
        self._stale = True
        self.action_clear()

    def set_repo(self, repo: Repository) -> None:
        """Point at a different cache entirely -- see `ObjectTree.set_repo`."""
        self.repo = repo
        self.set_database(None)

    def cache_updated(self) -> None:
        """Mark the corpus stale; it is rebuilt on the next keystroke."""
        self._stale = True

    def focus_query(self) -> None:
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
        self._choose(0)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self._choose(int(event.option.id))

    def action_to_results(self) -> None:
        if self._shown:
            self.query_one("#search-results", OptionList).focus()

    def action_clear(self) -> None:
        was_active = self.active
        self.query_one("#search-query", Input).value = ""
        self._shown = []
        results = self.query_one("#search-results", OptionList)
        results.clear_options()
        results.display = False
        if was_active:
            self.post_message(self.Activity(False))

    # -------------------------------------------------------------- ranking

    def _ensure_corpus(self) -> None:
        if not self._stale:
            return
        self._corpus = self.repo.search_corpus(self.database) if self.database else []
        self._stale = False

    def _run_query(self) -> None:
        query = self._query
        results = self.query_one("#search-results", OptionList)
        if not query:
            self._shown = []
            results.clear_options()
            results.display = False
            self.post_message(self.Activity(False))
            return

        self._ensure_corpus()
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
        results.display = True
        if self._shown:
            results.highlighted = 0
        self.post_message(self.Activity(True))

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
            self.post_message(ObjectSelected(self._shown[index]))
