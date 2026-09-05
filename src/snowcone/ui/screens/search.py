"""The object search modal: find-as-you-type over every cached object -- and
every cached column -- in the current database, given the room a modal can
take instead of a sliver of the top pane.

Both corpora are built once, when the dialog opens, from whatever is in the
cache right then -- there is no long-lived widget for a background cache
update to mark stale while the dialog is closed, and while it is open
`SnowconeApp` routes `CacheUpdated` to `self.screen`, which by then is this
dialog, not `MainScreen`, so a live update would not reach the tree/detail
pane either (the same is already true of the database picker). Closing and
reopening picks up whatever changed in between, which is the same cost a
keystroke used to pay to notice a stale corpus.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from snowcone.cache.repository import Repository
from snowcone.fuzzy import Match, highlight, rank, rank_qualified
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
        self._column_corpus: list[tuple[ObjectRef, str]] = []
        self._shown: list[ObjectRef] = []
        self._timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Input(placeholder="search objects", id="search-query")
            yield OptionList(id="search-results")

    def on_mount(self) -> None:
        self._corpus = self.repo.search_corpus(self.database) if self.database else []
        self._column_corpus = (
            self.repo.search_column_corpus(self.database) if self.database else []
        )
        self.query_one("#search-query", Input).focus()

    # --------------------------------------------------------------- events

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        # Debounced with a named timer: holding a key down would otherwise
        # rank the whole corpus once per character.
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(DEBOUNCE, self._run_query)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        # Enter confirms right away, so it must not act on a stale ranking
        # left over from a debounce that has not fired yet.
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        await self._run_query()
        self._choose(0)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self._choose(int(event.option.id))

    def action_to_results(self) -> None:
        if self._shown:
            self.query_one("#search-results", OptionList).focus()

    # -------------------------------------------------------------- ranking

    async def _run_query(self) -> None:
        query = self.query_one("#search-query", Input).value.strip()
        if not query:
            self._shown = []
            self.query_one("#search-results", OptionList).clear_options()
            return

        # Ranking is CPU-bound, not I/O, so it would otherwise run on and
        # block the event loop for the duration -- fine at the sizes measured
        # in fuzzy.py alone, but a column corpus routinely outnumbers the
        # object corpus 10-20x, which can push a keystroke's ranking time
        # into visibly-stuttering territory. Threaded so the loop stays free.
        ranked = await asyncio.to_thread(self._rank, query)
        # A newer keystroke may have already superseded this query while the
        # thread was running; drop a stale result rather than flash it.
        if self.query_one("#search-query", Input).value.strip() != query:
            return
        self._apply_results(ranked)

    def _rank(self, query: str) -> list[tuple[ObjectRef, Match, str | None]]:
        """Best hit per object, from either its own name or a column's.

        Pure and widget-free -- safe to call from a thread, since `_corpus`
        and `_column_corpus` are built once on mount and never mutated while
        the modal is open (see the module docstring).
        """
        # `rank`/`rank_qualified` break score ties by corpus order, which is
        # what keeps an empty query a plain passthrough; merging two ranked
        # lists through a set would throw that away, so first appearance
        # across either corpus is recorded up front and carried through the
        # merge as the same kind of tie-break.
        order: dict[ObjectRef, int] = {}
        for _, ref in self._corpus:
            order.setdefault(ref, len(order))
        for ref, _ in self._column_corpus:
            order.setdefault(ref, len(order))

        object_hits: dict[ObjectRef, Match] = dict(
            rank_qualified(
                query,
                ((ref.schema, ref.name, ref) for _, ref in self._corpus),
                limit=MAX_RESULTS,
            )
        )
        # Plain `rank`, not `rank_qualified`: there is no schema/name-style
        # qualification ambiguity to resolve for a column, so the simpler
        # matcher is enough, and its positions are already relative to the
        # bare column name, ready to highlight directly.
        column_hits: dict[ObjectRef, tuple[str, Match]] = {}
        for (ref, column), hit in rank(
            query,
            ((column, (ref, column)) for ref, column in self._column_corpus),
            limit=MAX_RESULTS,
        ):
            best = column_hits.get(ref)
            if best is None or hit.score > best[1].score:
                column_hits[ref] = (column, hit)

        merged: list[tuple[ObjectRef, Match, str | None]] = []
        for ref in object_hits.keys() | column_hits.keys():
            object_hit = object_hits.get(ref)
            column_name, column_hit = column_hits.get(ref, (None, None))
            if object_hit is not None and (
                column_hit is None or object_hit.score >= column_hit.score
            ):
                merged.append((ref, object_hit, None))
            else:
                assert column_hit is not None
                merged.append((ref, column_hit, column_name))
        merged.sort(key=lambda item: (-item[1].score, order[item[0]]))
        return merged[:MAX_RESULTS]

    def _apply_results(self, ranked: list[tuple[ObjectRef, Match, str | None]]) -> None:
        results = self.query_one("#search-results", OptionList)
        self._shown = [ref for ref, _, _ in ranked]
        results.clear_options()
        results.add_options(
            [
                Option(self._option_text(ref, hit, column), id=str(index))
                for index, (ref, hit, column) in enumerate(ranked)
            ]
        )
        if self._shown:
            results.highlighted = 0

    def _option_text(self, ref: ObjectRef, hit: Match, column: str | None) -> Text:
        # A Rich Text cannot reference a CSS variable, so the theme's accent
        # has to be resolved to a real colour here.
        accent = self.app.theme_variables.get("accent")
        style = f"bold {accent}" if accent else "bold"
        text = Text()
        # A column-matched hit's positions are relative to the column name,
        # not "schema.name", so there is nothing to highlight there.
        name_positions = hit.positions if column is None else ()
        for run, matched in highlight(f"{ref.schema}.{ref.name}", name_positions):
            text.append(run, style=style if matched else "")
        text.append(f"  {ref.kind.value.lower()}", style="dim")
        if column is not None:
            text.append("  · ", style="dim")
            for run, matched in highlight(column, hit.positions):
                text.append(run, style=style if matched else "dim")
        return text

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self._shown):
            self.dismiss(self._shown[index])
