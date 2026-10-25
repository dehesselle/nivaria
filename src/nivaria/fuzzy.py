# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Subsequence fuzzy matching, fzy-style. Pure: no Textual, no I/O.

Scanning names in Python rather than in sqlite is deliberate. FTS5 -- even
with the trigram tokenizer available here -- does substring matching, not
subsequence: it would not match "stgord" against "STG_CUSTOMER_ORDERS", which
is exactly the typing shorthand this is for.

Measured cost of ranking a whole corpus, worst query, on the machine this was
written on: 2k objects 3ms, 10k 14ms, 50k 70ms, 100k 150ms. The first two are
comfortably inside a keystroke budget and the search box debounces on top of
that. Beyond roughly 20k objects in a single database this would want moving
off the event loop into a thread -- it is a plain function precisely so that
stays an easy change.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Bonuses are relative to each other only; the absolute scale is meaningless.
_ADJACENT = 12.0
_BOUNDARY = 10.0
_CAMEL = 8.0
_LEADING_GAP_PENALTY = 0.4
_MAX_LEADING_GAP = 8

_SEPARATORS = frozenset("_-. /\\:")


@dataclass(frozen=True)
class Match:
    """A scored hit, with the positions that matched so they can be shown."""

    score: float
    positions: tuple[int, ...]


def _is_boundary(candidate: str, index: int) -> bool:
    if index == 0:
        return True
    return candidate[index - 1] in _SEPARATORS


def _is_camel(candidate: str, index: int) -> bool:
    if index == 0:
        return False
    return candidate[index - 1].islower() and candidate[index].isupper()


def match(query: str, candidate: str) -> Match | None:
    """Score `candidate` against `query`, or None if it is not a subsequence.

    Greedy left-to-right: the first position that can match each query
    character is taken. That is not always the highest-scoring alignment, but
    it is linear and the difference is not visible in a picker where the
    ranking, not the exact score, is what the user sees.
    """
    if not query:
        return Match(score=0.0, positions=())

    lowered_query = query.lower()
    lowered_candidate = candidate.lower()

    positions: list[int] = []
    score = 0.0
    index = 0
    previous = -1

    for character in lowered_query:
        found = lowered_candidate.find(character, index)
        if found == -1:
            return None
        if previous >= 0 and found == previous + 1:
            score += _ADJACENT
        if _is_boundary(candidate, found):
            score += _BOUNDARY
        elif _is_camel(candidate, found):
            score += _CAMEL
        positions.append(found)
        previous = found
        index = found + 1

    # A hit near the start of the name is usually the one meant; the penalty is
    # capped so a long prefix does not swamp the structural bonuses above.
    score -= min(positions[0], _MAX_LEADING_GAP) * _LEADING_GAP_PENALTY
    # Shorter names win ties: "ORDERS" before "ORDERS_ARCHIVE_2019".
    score -= len(candidate) * 0.01
    return Match(score=score, positions=tuple(positions))


def rank[T](
    query: str,
    candidates: Iterable[tuple[str, T]],
    *,
    limit: int = 200,
) -> list[tuple[T, Match]]:
    """Best `limit` candidates for `query`, highest score first.

    Each candidate is `(text_to_match, payload)`. An empty query keeps the
    input order, so a picker opens showing everything rather than nothing.
    """
    scored: list[tuple[float, int, T, Match]] = []
    for order, (text, payload) in enumerate(candidates):
        hit = match(query, text)
        if hit is None:
            continue
        # `order` keeps the sort stable and preserves input order on ties,
        # which is what makes an empty query a plain passthrough.
        scored.append((-hit.score, order, payload, hit))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [(payload, hit) for _, _, payload, hit in scored[:limit]]


def highlight(text: str, positions: Sequence[int]) -> list[tuple[str, bool]]:
    """Split `text` into (run, matched) pairs for rendering."""
    if not positions:
        return [(text, False)] if text else []
    marked = set(positions)
    runs: list[tuple[str, bool]] = []
    start = 0
    current = 0 in marked
    for index in range(1, len(text) + 1):
        is_match = index in marked if index < len(text) else not current
        if index == len(text) or is_match != current:
            runs.append((text[start:index], current))
            start = index
            current = is_match
    return runs


#: A hit contained entirely within the object's own name beats one spread
#: across schema and name. Small, so it only settles otherwise-close calls.
_NAME_BONUS = 2.0


def match_qualified(query: str, prefix: str, name: str) -> Match | None:
    """Match `prefix.name`, also trying the bare name, and keep the better.

    Snowflake schemas tend to repeat themselves in their objects' names -- a
    STAGING schema full of STG_ tables. Greedy left-to-right matching then
    spends the query's leading characters inside the schema and is left with a
    poor alignment in the name: "stgord" scores STAGING.LOAD_ORDERS *above*
    STAGING.STG_CUSTOMER_ORDERS, which is not what anyone typing it meant.

    Scoring the bare name as well, with positions shifted back into the
    qualified string so highlighting still lines up, recovers the intended
    reading without the cost of a full alignment search.
    """
    qualified = f"{prefix}.{name}"
    best = match(query, qualified)
    on_name = match(query, name)
    if on_name is not None:
        offset = len(prefix) + 1
        shifted = Match(
            score=on_name.score + _NAME_BONUS,
            positions=tuple(position + offset for position in on_name.positions),
        )
        if best is None or shifted.score > best.score:
            best = shifted
    return best


def split_query(query: str) -> tuple[str, tuple[str, ...]]:
    """Split a typed query into the word that ranks and the words that filter.

    Only the first word is scored. Every later word narrows the results
    without re-scoring them, so adding one can never reorder the list or bring
    a row back. Letting every word score equally, fzf-style, was rejected: a
    lone word must keep the database out of matching (see docs/DESIGN.md "Matching
    and ranking"), so a second word would change what the first one may
    match, and the list could grow or reshuffle as the user typed more.
    """
    first, *rest = query.split() or [""]
    return first, tuple(rest)


def match_segment(token: str, segments: Sequence[str]) -> tuple[int, Match] | None:
    """The best segment `token` matches inside, as `(index, match)`, or None.

    One segment at a time rather than the dotted whole: across
    `DB.SCHEMA.NAME` a subsequence like `test_db` can borrow letters from all
    three, which matches nearly everything and narrows nothing.
    """
    best: tuple[int, Match] | None = None
    for index, segment in enumerate(segments):
        hit = match(token, segment)
        if hit is not None and (best is None or hit.score > best[1].score):
            best = (index, hit)
    return best


def rank_qualified[T](
    query: str,
    candidates: Iterable[tuple[str, str, T]],
    *,
    limit: int = 200,
) -> list[tuple[T, Match]]:
    """Rank `(prefix, name, payload)` triples by `match_qualified`."""
    scored: list[tuple[float, int, T, Match]] = []
    for order, (prefix, name, payload) in enumerate(candidates):
        hit = match_qualified(query, prefix, name)
        if hit is None:
            continue
        scored.append((-hit.score, order, payload, hit))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [(payload, hit) for _, _, payload, hit in scored[:limit]]
