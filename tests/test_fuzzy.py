# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

from nivaria.fuzzy import (
    highlight,
    match,
    match_qualified,
    match_segment,
    rank,
    rank_qualified,
    split_query,
)


def names(*items: str) -> list[tuple[str, str]]:
    return [(item, item) for item in items]


def ordered(query: str, *items: str) -> list[str]:
    return [payload for payload, _ in rank(query, names(*items))]


# ---------------------------------------------------------------- matching


def test_an_exact_substring_matches() -> None:
    assert match("orders", "ORDERS") is not None


def test_matching_ignores_case() -> None:
    assert match("ORD", "stg_orders") is not None


def test_a_subsequence_matches_even_when_scattered() -> None:
    """The typing shorthand this exists for: 'stgord' should find
    STG_CUSTOMER_ORDERS, which no substring search would."""
    assert match("stgord", "STG_CUSTOMER_ORDERS") is not None


def test_characters_must_appear_in_order() -> None:
    assert match("sredro", "ORDERS") is None


def test_a_missing_character_does_not_match() -> None:
    assert match("orderz", "ORDERS") is None


def test_an_empty_query_matches_everything() -> None:
    hit = match("", "ANYTHING")
    assert hit is not None
    assert hit.positions == ()


def test_positions_point_at_the_matched_characters() -> None:
    hit = match("od", "ORDERS")
    assert hit is not None
    assert "".join("ORDERS"[index] for index in hit.positions) == "OD"


def test_positions_are_ascending() -> None:
    hit = match("stgord", "STG_CUSTOMER_ORDERS")
    assert hit is not None
    assert list(hit.positions) == sorted(hit.positions)


# ----------------------------------------------------------------- ranking


def test_a_word_boundary_beats_a_match_buried_mid_word() -> None:
    """Snowflake names are underscore-separated, so a hit at the start of a
    part is almost always the one meant."""
    assert ordered("ord", "STG_ORDERS", "RECORDING")[0] == "STG_ORDERS"


def test_adjacent_characters_beat_scattered_ones() -> None:
    assert ordered("abc", "ABC_TOTAL", "A_B_C_TOTAL")[0] == "ABC_TOTAL"


def test_a_shorter_name_wins_a_tie() -> None:
    assert ordered("orders", "ORDERS", "ORDERS_ARCHIVE_2019")[0] == "ORDERS"


def test_an_earlier_match_beats_a_later_one() -> None:
    assert ordered("x", "XRAY", "VERY_LONG_PREFIX_X")[0] == "XRAY"


def test_non_matches_are_dropped_entirely() -> None:
    assert ordered("zzz", "ORDERS", "CUSTOMERS") == []


def test_an_empty_query_keeps_input_order() -> None:
    """A picker opens showing everything, in the order it was given."""
    assert ordered("", "B", "A", "C") == ["B", "A", "C"]


def test_the_limit_is_respected() -> None:
    results = rank("a", names(*[f"A{i}" for i in range(50)]), limit=5)
    assert len(results) == 5


def test_ranking_returns_the_payload_not_the_text() -> None:
    """Callers match on a display string but want the object back."""
    results = rank("ord", [("MYDB.PUBLIC.ORDERS", {"id": 7})])
    assert results[0][0] == {"id": 7}


def test_ties_keep_input_order() -> None:
    assert ordered("a", "A_ONE", "A_TWO") == ["A_ONE", "A_TWO"]


def test_ranking_an_empty_corpus_is_empty() -> None:
    assert rank("anything", []) == []


# -------------------------------------------------------------- highlight


def test_highlight_marks_the_matched_runs() -> None:
    assert highlight("ORDERS", (0, 1)) == [("OR", True), ("DERS", False)]


def test_highlight_handles_a_gap() -> None:
    assert highlight("ORDERS", (0, 3)) == [
        ("O", True),
        ("RD", False),
        ("E", True),
        ("RS", False),
    ]


def test_highlight_with_no_positions_is_one_plain_run() -> None:
    assert highlight("ORDERS", ()) == [("ORDERS", False)]


def test_highlight_of_an_empty_string_is_nothing() -> None:
    assert highlight("", ()) == []


def test_highlight_covering_everything_is_one_matched_run() -> None:
    assert highlight("ABC", (0, 1, 2)) == [("ABC", True)]


def test_highlight_runs_reassemble_into_the_original() -> None:
    text = "STG_CUSTOMER_ORDERS"
    hit = match("stgord", text)
    assert hit is not None
    assert "".join(run for run, _ in highlight(text, hit.positions)) == text


# ------------------------------------------------------- qualified matching


def test_a_schema_prefix_does_not_steal_the_match() -> None:
    """A STAGING schema full of STG_ tables is the common Snowflake case, and
    greedy matching spends 'stg' inside the schema, leaving a poor alignment
    in the name -- ranking LOAD_ORDERS above STG_CUSTOMER_ORDERS."""
    candidates = [
        ("STAGING", "LOAD_ORDERS", "load"),
        ("STAGING", "STG_CUSTOMER_ORDERS", "stg"),
    ]
    assert next(p for p, _ in rank_qualified("stgord", candidates)) == "stg"


def test_a_qualified_query_still_matches_across_the_dot() -> None:
    """Typing the schema has to keep working; the name is only preferred when
    it genuinely matches better."""
    candidates = [
        ("SALES", "ORDERS", "sales"),
        ("STAGING", "LOAD_ORDERS", "staging"),
    ]
    assert next(p for p, _ in rank_qualified("salord", candidates)) == "sales"


def test_positions_from_a_name_match_land_in_the_qualified_string() -> None:
    """Highlighting is drawn over 'SCHEMA.NAME', so shifted positions have to
    point at the right characters there."""
    hit = match_qualified("ord", "SALES", "ORDERS")
    assert hit is not None
    qualified = "SALES.ORDERS"
    assert "".join(qualified[i] for i in hit.positions).lower() == "ord"


def test_a_candidate_matching_neither_form_is_dropped() -> None:
    assert match_qualified("zzz", "SALES", "ORDERS") is None


def test_a_name_only_match_is_kept_when_the_qualified_form_fails() -> None:
    hit = match_qualified("ord", "X", "ORDERS")
    assert hit is not None


def test_rank_qualified_respects_the_limit() -> None:
    candidates = [("S", f"ORDERS_{i}", i) for i in range(20)]
    assert len(rank_qualified("ord", candidates, limit=3)) == 3


# ------------------------------------------------------ multi-word queries


def test_a_query_splits_into_a_ranking_word_and_filter_words() -> None:
    assert split_query("orders sales mydb") == ("orders", ("sales", "mydb"))


def test_a_single_word_query_has_no_filter_words() -> None:
    assert split_query("orders") == ("orders", ())


def test_surplus_whitespace_produces_no_empty_filter_words() -> None:
    assert split_query("  orders   sales  ") == ("orders", ("sales",))


def test_an_empty_query_splits_into_nothing() -> None:
    assert split_query("   ") == ("", ())


def test_a_filter_word_matches_inside_a_single_segment() -> None:
    found = match_segment("tstdb", ("TEST_DB", "SALES", "ORDERS"))
    assert found is not None
    index, hit = found
    assert index == 0
    assert "".join("TEST_DB"[i] for i in hit.positions).lower() == "tstdb"


def test_a_filter_word_may_not_straddle_segments() -> None:
    """Across the dotted whole, 'test_db' could take TEST_ from the schema
    and D/B from elsewhere -- which is how a filter ends up matching nearly
    everything."""
    segments = ("MYDB", "TEST_SCHEMA", "DB_ORDERS")
    assert match("test_db", ".".join(segments)) is not None
    assert match_segment("test_db", segments) is None


def test_a_filter_word_matching_no_segment_is_rejected() -> None:
    assert match_segment("zzz", ("MYDB", "SALES", "ORDERS")) is None


def test_a_filter_word_keeps_its_best_segment() -> None:
    found = match_segment("sales", ("SALES_DB", "SALES", "ORDERS"))
    assert found is not None
    assert found[0] == 1
