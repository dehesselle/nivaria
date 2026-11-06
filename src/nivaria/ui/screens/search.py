# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The object search modal: find-as-you-type over every cached object and
schema across every currently selected database, given the room a modal can
take instead of a sliver of the top pane.

Both corpora are built once, when the dialog opens, from whatever is in the
cache right then -- there is no long-lived widget for a background cache
update to mark stale while the dialog is closed, and while it is open
`NivariaApp` routes `CacheUpdated` to `self.screen`, which by then is this
dialog, not `MainScreen`, so a live update would not reach the tree/detail
pane either (the same is already true of the database picker). Closing and
reopening picks up whatever changed in between.

Loading is kicked off in `on_mount` as a task, not awaited there. `_run_query`
does not wait for it either: a fast typist would feel that wait on every
keystroke while the load is still pending, so ranking proceeds against whatever
corpus already exists, and the load re-runs the last query once it lands, so the
list corrects itself in place rather than a keystroke ever blocking on it. Enter
is the one exception: choosing a result and closing the dialog is a commitment a
still-empty corpus should not get to make, so that path alone waits for the load
first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from tcoda.dialogs import Dialog
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from nivaria.cache.repository import Repository
from nivaria.fuzzy import (
    Match,
    highlight,
    match_segment,
    rank,
    rank_qualified,
    split_query,
)
from nivaria.models import ObjectRef, SchemaRef

#: What the modal hands back: an object or a schema. A schema is never an
#: ObjectRef with some placeholder type -- see SchemaRef -- so the two are kept
#: as a plain union rather than forcing a schema to pretend to be a kind of
#: object it is not.
Hit = ObjectRef | SchemaRef

#: Long enough that holding a key down does not rank once per character,
#: short enough to feel immediate.
DEBOUNCE = 0.12

#: A screenful and then some, even at 90% of a large terminal.
MAX_RESULTS = 200


def _passes(filters: Sequence[str], segments: Sequence[str]) -> bool:
    """Whether every filter word matches inside one of `segments`."""
    return all(match_segment(token, segments) is not None for token in filters)


class SearchDialog(Dialog[Hit]):
    """Pick one object or schema by fuzzy name, or dismiss with None."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("down", "to_results", "Results", show=False),
    ]

    def __init__(self, repo: Repository, databases: Sequence[str]) -> None:
        super().__init__()
        self.repo = repo
        self.databases = list(databases)
        self._corpus: list[tuple[str, ObjectRef]] = []
        self._schema_corpus: list[SchemaRef] = []
        self._shown: list[Hit] = []
        self._filters: tuple[str, ...] = ()
        self._timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Input(
                placeholder="search objects -- more words narrow by schema or database",
                id="search-query",
            )
            yield OptionList(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-query", Input).focus()
        self._loaded: asyncio.Task[None] = asyncio.create_task(self._load())

    def on_unmount(self) -> None:
        # Otherwise a slow load still in flight when the dialog closes would
        # resume after unmount and touch widgets that no longer exist.
        self._loaded.cancel()

    async def _load(self) -> None:
        self._corpus, self._schema_corpus = await asyncio.to_thread(self._read)
        await self._refresh_if_typing()

    def _read(self) -> tuple[list[tuple[str, ObjectRef]], list[SchemaRef]]:
        """Pure: no widget access, so safe to call from a thread."""
        if not self.databases:
            return [], []
        return (
            self.repo.search_corpus(self.databases),
            self.repo.search_schema_corpus(self.databases),
        )

    async def _refresh_if_typing(self) -> None:
        # A quick typist can have a query in the box before the load finishes
        # -- _run_query never waits for it (see there), so whatever it showed
        # was ranked against an empty corpus. Re-running now replaces that. A
        # fresh keystroke arriving in between just makes this redundant, not
        # wrong: _run_query's own stale-result check means only the most recent
        # one actually renders.
        if self.query_one("#search-query", Input).value.strip():
            await self._run_query()

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
        # Enter commits to a choice and closes the dialog, unlike a plain
        # keystroke's list refresh -- so unlike _run_query's live-typing path,
        # this one waits for the load rather than risk closing on a result
        # ranked against an empty corpus.
        await self._loaded
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

        # Deliberately not waiting for the load here: a fast typist who opens
        # search and starts typing immediately would otherwise feel that wait
        # on every keystroke while it is still pending. Ranking proceeds
        # against whatever the corpora currently are, and _refresh_if_typing
        # re-runs this once the load completes, so the list corrects itself
        # rather than the keystroke blocking on it.
        #
        # Ranking is CPU-bound, not I/O, so it would otherwise run on and
        # block the event loop for the duration, which a large selection can
        # make visible (docs/measurements.md "Search"). Threaded so the loop
        # stays free.
        ranked = await asyncio.to_thread(self._rank, query)
        # A newer keystroke may have already superseded this query while the
        # thread was running; drop a stale result rather than flash it.
        if self.query_one("#search-query", Input).value.strip() != query:
            return
        self._apply_results(ranked, split_query(query)[1])

    def _rank(self, query: str) -> list[tuple[Hit, Match]]:
        """Best hits among the objects and schemas.

        Only the query's first word ranks; any further words are filters (see
        `split_query`). They are applied to the corpora *before* ranking, not
        to its output: ranking stops at `MAX_RESULTS`, and a name repeated
        across enough databases would push the one the filters are after past
        that cut before they ever saw it. Each query is also ranked from
        scratch rather than by narrowing the previous result list, which would
        break on backspace, a mid-query edit or a paste, and inherit the same
        cut.

        Pure and widget-free -- safe to call from a thread. The corpora do get
        reassigned once, when the load finishes, but never mutated in place,
        and a plain attribute reassignment is atomic under the GIL -- so a read
        from this thread always sees one complete corpus or the other, never a
        partial write.
        """
        query, filters = split_query(query)

        objects = [
            ref
            for _, ref in self._corpus
            if _passes(filters, (ref.database, ref.schema, ref.name))
        ]
        schemas = [
            schema
            for schema in self._schema_corpus
            if _passes(filters, (schema.database, schema.name))
        ]

        # `rank`/`rank_qualified` break score ties by corpus order, which is
        # what keeps an empty query a plain passthrough; merging the two ranked
        # lists would throw that away, so each hit's corpus position is carried
        # through the merge as the same kind of tie-break.
        order: dict[Hit, int] = {
            hit: index for index, hit in enumerate((*objects, *schemas))
        }
        merged: list[tuple[Hit, Match]] = [
            *rank_qualified(
                query,
                ((ref.schema, ref.name, ref) for ref in objects),
                limit=MAX_RESULTS,
            ),
            # Plain `rank`, not `rank_qualified`: there is no schema/name-style
            # qualification ambiguity to resolve for a bare schema name, so the
            # simpler matcher is enough.
            *rank(
                query,
                ((schema.name, schema) for schema in schemas),
                limit=MAX_RESULTS,
            ),
        ]
        merged.sort(key=lambda item: (-item[1].score, order[item[0]]))
        return merged[:MAX_RESULTS]

    def _apply_results(
        self, ranked: list[tuple[Hit, Match]], filters: tuple[str, ...]
    ) -> None:
        results = self.query_one("#search-results", OptionList)
        self._shown = [ref for ref, _ in ranked]
        self._filters = filters
        results.clear_options()
        results.add_options(
            [
                Option(self._option_text(ref, hit), id=str(index))
                for index, (ref, hit) in enumerate(ranked)
            ]
        )
        if self._shown:
            results.highlighted = 0

    def _option_text(self, ref: Hit, hit: Match) -> Text:
        # A Rich Text cannot reference a CSS variable, so the theme's accent
        # has to be resolved to a real colour here.
        accent = self.app.theme_variables.get("accent")
        style = f"bold {accent}" if accent else "bold"
        text = Text()
        if isinstance(ref, SchemaRef):
            # The database is a dim prefix -- several can be active at once,
            # so it has to be shown, but it never competes for ranking (see
            # rank_qualified below); only a filter word lights it up.
            database_marks, name_marks = self._filter_marks((ref.database, ref.name))
            for run, matched in highlight(ref.database, sorted(database_marks)):
                text.append(run, style=style if matched else "dim")
            text.append(".", style="dim")
            marks = name_marks.union(hit.positions)
            for run, matched in highlight(ref.name, sorted(marks)):
                text.append(run, style=style if matched else "")
            text.append("  schema", style="dim")
            return text
        # Database prefix added at render time only, deliberately outside
        # rank_qualified's schema/name qualification below -- extending
        # ranking to a third level would spend the query's leading
        # characters on the database too, and there is no
        # database-repeats-inside-object-name ambiguity the way schema/
        # STAGING.STG_ has to justify that cost. Filter words may match it,
        # though, and are highlighted so a row shows why it survived.
        database_marks, schema_marks, name_marks = self._filter_marks(
            (ref.database, ref.schema, ref.name)
        )
        for run, matched in highlight(ref.database, sorted(database_marks)):
            text.append(run, style=style if matched else "dim")
        text.append(".", style="dim")
        offset = len(ref.schema) + 1
        marks = schema_marks.union(hit.positions, (p + offset for p in name_marks))
        for run, matched in highlight(f"{ref.schema}.{ref.name}", sorted(marks)):
            text.append(run, style=style if matched else "")
        text.append(f"  {ref.type.value.lower()}", style="dim")
        return text

    def _filter_marks(self, segments: Sequence[str]) -> list[set[int]]:
        """Per segment, the positions the current filter words matched.

        Recomputed here rather than carried out of `_rank`: it is at most
        `MAX_RESULTS` rows, and it keeps the ranking result's shape free of
        something only rendering needs.
        """
        marks: list[set[int]] = [set() for _ in segments]
        for token in self._filters:
            found = match_segment(token, segments)
            if found is not None:
                index, hit = found
                marks[index].update(hit.positions)
        return marks

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self._shown):
            self.dismiss(self._shown[index])
