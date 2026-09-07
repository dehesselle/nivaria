"""The object search modal: find-as-you-type over every cached object and
schema across every currently selected database, given the room a modal can
take instead of a sliver of the top pane.

Column search is currently disabled -- see `on_mount`'s commented-out
`_load_columns()` call and DESIGN.md "Known gaps". The reader, the ranking
logic below, and `MIN_COLUMN_QUERY_LENGTH` are left in place for when it is
re-enabled; this docstring still describes them as if live, since the code
they describe is unchanged, just not currently invoked.

Both corpora are built once, when the dialog opens, from whatever is in the
cache right then -- there is no long-lived widget for a background cache
update to mark stale while the dialog is closed, and while it is open
`SnowconeApp` routes `CacheUpdated` to `self.screen`, which by then is this
dialog, not `MainScreen`, so a live update would not reach the tree/detail
pane either (the same is already true of the database picker). Closing and
reopening picks up whatever changed in between, which is the same cost a
keystroke used to pay to notice a stale corpus.

Loading is kicked off in `on_mount` as independent tasks, none awaited there
and all real work rather than a formality: the object corpus alone is cheap
(~5ms even at 2000 objects), but the column corpus was measured at ~55-75ms
for 30k cached columns -- and fetching them together meant the cheap ones
waited on the expensive one every time, since a single call blocks on
whichever finishes last. Split, name and schema matches become available
almost as soon as the dialog paints, with column matches joining in once
their own load lands (when that load runs at all -- see above, it is
currently disabled). `_run_query` does not wait for any of them: a fast
typist would feel that wait on every keystroke while a load is still
pending, so ranking proceeds against whatever corpus already exists --
empty, partial, or complete -- and each load re-runs the last query once it
lands, so the list corrects itself in place rather than a keystroke ever
blocking on it. Enter is the one exception: choosing a result and closing the
dialog is a commitment a still-partial corpus should not get to make, so that
path alone waits for every (live) load first.

Ranking itself also skips the column corpus below `MIN_COLUMN_QUERY_LENGTH`
characters: a short, generic fragment scores against every cached column for
little relevance benefit and, at a heavily pre-warmed scale (270k columns
measured), real per-keystroke latency -- see that constant for the number.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from tcw.screens import DialogScreen
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from snowcone.cache.repository import Repository
from snowcone.fuzzy import Match, highlight, rank, rank_qualified
from snowcone.models import ObjectRef, SchemaRef

#: What the modal hands back: an object, a column's owning object, or a
#: schema. A schema is never an ObjectRef with some placeholder kind -- see
#: SchemaRef -- so the two are kept as a plain union rather than forcing a
#: schema to pretend to be a kind of object it is not.
Hit = ObjectRef | SchemaRef

#: Long enough that holding a key down does not rank once per character,
#: short enough to feel immediate.
DEBOUNCE = 0.12

#: A screenful and then some, even at 90% of a large terminal.
MAX_RESULTS = 200

#: Below this many characters, ranking skips the column corpus entirely --
#: object names are still matched at any length. A short, generic fragment
#: scores against every cached column for little relevance benefit and, on a
#: heavily pre-warmed database, real latency: measured at 270k cached columns,
#: a 4-character query ("col1") alone cost ~450ms of ranking. A plain module
#: constant rather than a Setting on purpose -- this is a tuning knob for a
#: specific machine's cache size, not a per-user preference worth a UI entry.
MIN_COLUMN_QUERY_LENGTH = 3


class SearchScreen(DialogScreen[Hit]):
    """Pick one object or schema by fuzzy name, or dismiss with None."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("down", "to_results", "Results", show=False),
    ]

    def __init__(self, repo: Repository, databases: Sequence[str]) -> None:
        super().__init__()
        self.repo = repo
        self.databases = list(databases)
        self._corpus: list[tuple[str, ObjectRef]] = []
        self._column_corpus: list[tuple[ObjectRef, str]] = []
        self._schema_corpus: list[SchemaRef] = []
        self._shown: list[Hit] = []
        self._timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Input(placeholder="search objects", id="search-query")
            yield OptionList(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-query", Input).focus()
        # Three independent loads, not one: the object and schema corpora are
        # both cheap (~5ms even at 2000 objects; a schema list is smaller
        # still) but were measured stuck for ~55-75ms behind the column
        # corpus when fetched together, since a call that reads all of them
        # blocks on whichever is slowest. Splitting them means name and
        # schema matches become available almost immediately, with column
        # matches joining in a beat later, instead of all three waiting on
        # the slowest one. None is awaited here -- see _load_objects et al.
        self._objects_loaded: asyncio.Task[None] = asyncio.create_task(
            self._load_objects()
        )
        # Column search is disabled for now -- cross-database column search
        # is not ready for prime time. The reader (_read_columns), _rank's
        # column-handling logic, and MIN_COLUMN_QUERY_LENGTH are all left in
        # place; only this call is commented out. See DESIGN.md "Known gaps".
        self._columns_loaded: asyncio.Task[None] | None = None
        # self._columns_loaded = asyncio.create_task(self._load_columns())
        self._schemas_loaded: asyncio.Task[None] = asyncio.create_task(
            self._load_schemas()
        )

    def on_unmount(self) -> None:
        # Otherwise a slow load still in flight when the dialog closes would
        # resume after unmount and touch widgets that no longer exist.
        self._objects_loaded.cancel()
        if self._columns_loaded is not None:
            self._columns_loaded.cancel()
        self._schemas_loaded.cancel()

    async def _load_objects(self) -> None:
        self._corpus = await asyncio.to_thread(self._read_objects)
        await self._refresh_if_typing()

    async def _load_columns(self) -> None:
        self._column_corpus = await asyncio.to_thread(self._read_columns)
        await self._refresh_if_typing()

    async def _load_schemas(self) -> None:
        self._schema_corpus = await asyncio.to_thread(self._read_schemas)
        await self._refresh_if_typing()

    def _read_objects(self) -> list[tuple[str, ObjectRef]]:
        """Pure: no widget access, so safe to call from a thread."""
        return self.repo.search_corpus(self.databases) if self.databases else []

    def _read_columns(self) -> list[tuple[ObjectRef, str]]:
        """Pure: no widget access, so safe to call from a thread. Currently
        unreachable -- see on_mount's commented-out _load_columns() call."""
        return self.repo.search_column_corpus(self.databases) if self.databases else []

    def _read_schemas(self) -> list[SchemaRef]:
        """Pure: no widget access, so safe to call from a thread."""
        return self.repo.search_schema_corpus(self.databases) if self.databases else []

    async def _refresh_if_typing(self) -> None:
        # A quick typist can have a query in the box before either load
        # finishes -- _run_query never waits for them (see there), so
        # whatever it showed was ranked against a partial corpus. Re-running
        # now replaces that with the fuller one. A fresh keystroke arriving
        # in between just makes this redundant, not wrong: _run_query's own
        # stale-result check means only the most recent one actually renders.
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
        # this one waits for all three loads rather than risk picking (or
        # closing on) a result ranked against one still partially empty.
        await self._objects_loaded
        if self._columns_loaded is not None:
            await self._columns_loaded
        await self._schemas_loaded
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

        # Deliberately not waiting for either load here: a fast typist who
        # opens search and starts typing immediately would otherwise feel
        # that wait on every keystroke while one is still pending. Ranking
        # proceeds against whatever self._corpus/_column_corpus currently are
        # -- empty or partial before a load finishes -- and _refresh_if_typing
        # re-runs this once each one completes, so the list corrects itself
        # rather than the keystroke blocking on it.
        #
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

    def _rank(self, query: str) -> list[tuple[Hit, Match, str | None]]:
        """Best hit per object or schema, from a name, a column, or itself.

        Pure and widget-free -- safe to call from a thread. `_corpus`,
        `_column_corpus` and `_schema_corpus` do get reassigned once each,
        when the corresponding background load in `_load_objects`/
        `_load_columns`/`_load_schemas` finishes, but never mutated in
        place, and a plain attribute reassignment is atomic under the GIL --
        so a read from this thread always sees one complete corpus or the
        other, never a partial write (see the module docstring for why this
        runs against a possibly-incomplete corpus at all).
        """
        search_columns = len(query) >= MIN_COLUMN_QUERY_LENGTH

        # `rank`/`rank_qualified` break score ties by corpus order, which is
        # what keeps an empty query a plain passthrough; merging ranked lists
        # through a set would throw that away, so first appearance across
        # any corpus is recorded up front and carried through the merge as
        # the same kind of tie-break. Skipped for the column corpus below the
        # length gate, same as the ranking itself -- otherwise a short query
        # would still pay for walking all of it just for this.
        order: dict[Hit, int] = {}
        for _, ref in self._corpus:
            order.setdefault(ref, len(order))
        if search_columns:
            for ref, _ in self._column_corpus:
                order.setdefault(ref, len(order))
        for schema in self._schema_corpus:
            order.setdefault(schema, len(order))

        object_hits: dict[ObjectRef, Match] = dict(
            rank_qualified(
                query,
                ((ref.schema, ref.name, ref) for _, ref in self._corpus),
                limit=MAX_RESULTS,
            )
        )
        # Plain `rank`, not `rank_qualified`, for both of these: there is no
        # schema/name-style qualification ambiguity to resolve for a bare
        # column or schema name, so the simpler matcher is enough, and its
        # positions are already relative to the name itself, ready to
        # highlight directly.
        column_hits: dict[ObjectRef, tuple[str, Match]] = {}
        if search_columns:
            for (ref, column), hit in rank(
                query,
                ((column, (ref, column)) for ref, column in self._column_corpus),
                limit=MAX_RESULTS,
            ):
                best = column_hits.get(ref)
                if best is None or hit.score > best[1].score:
                    column_hits[ref] = (column, hit)
        schema_hits: dict[SchemaRef, Match] = dict(
            rank(
                query,
                ((schema.name, schema) for schema in self._schema_corpus),
                limit=MAX_RESULTS,
            )
        )

        merged: list[tuple[Hit, Match, str | None]] = []
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
        # Schemas never compete with objects/columns for the same Hit -- an
        # ObjectRef and a SchemaRef are never equal -- so these just join the
        # merge rather than needing a best-of comparison like the above.
        for schema, hit in schema_hits.items():
            merged.append((schema, hit, None))
        merged.sort(key=lambda item: (-item[1].score, order[item[0]]))
        return merged[:MAX_RESULTS]

    def _apply_results(self, ranked: list[tuple[Hit, Match, str | None]]) -> None:
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

    def _option_text(self, ref: Hit, hit: Match, column: str | None) -> Text:
        # A Rich Text cannot reference a CSS variable, so the theme's accent
        # has to be resolved to a real colour here.
        accent = self.app.theme_variables.get("accent")
        style = f"bold {accent}" if accent else "bold"
        text = Text()
        if isinstance(ref, SchemaRef):
            # The database is a plain, unhighlighted prefix -- several can be
            # active at once now, so it has to be shown, but it never
            # competed for ranking (see rank_qualified below), so there is
            # nothing to highlight in it.
            text.append(f"{ref.database}.", style="dim")
            for run, matched in highlight(ref.name, hit.positions):
                text.append(run, style=style if matched else "")
            text.append("  schema", style="dim")
            return text
        # A column-matched hit's positions are relative to the column name,
        # not "schema.name", so there is nothing to highlight there.
        name_positions = hit.positions if column is None else ()
        # Database prefix added at render time only, deliberately outside
        # rank_qualified's schema/name qualification below -- extending
        # ranking to a third level would spend the query's leading
        # characters on the database too, and there is no
        # database-repeats-inside-object-name ambiguity the way schema/
        # STAGING.STG_ has to justify that cost.
        text.append(f"{ref.database}.", style="dim")
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
