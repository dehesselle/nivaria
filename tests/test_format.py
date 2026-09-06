from __future__ import annotations

import pytest

from snowcone.format import (
    format_age,
    format_bytes,
    format_count,
    format_data_type,
    truncate_middle,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "never"),
        (0, "0s"),
        (12, "12s"),
        (59, "59s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86399, "23h"),
        (86400, "1d"),
    ],
)
def test_format_age_picks_the_largest_fitting_unit(seconds, expected) -> None:
    assert format_age(seconds) == expected


def test_format_age_clamps_negative_ages() -> None:
    """Clock skew between the cache timestamp and now must not print '-3s'."""
    assert format_age(-5) == "0s"


def test_format_count_is_parenthesised() -> None:
    assert format_count(40) == "(40)"


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, ""),
        (0, "0B"),
        (512, "512B"),
        (1024, "1.0K"),
        (1536, "1.5K"),
        (1048576, "1.0M"),
    ],
)
def test_format_bytes(size, expected) -> None:
    assert format_bytes(size) == expected


def test_format_data_type_renders_text_as_varchar_with_length() -> None:
    assert (
        format_data_type("TEXT", character_maximum_length=16777216)
        == "VARCHAR(16777216)"
    )


def test_format_data_type_renders_fixed_as_number_with_precision_and_scale() -> None:
    assert (
        format_data_type("FIXED", numeric_precision=38, numeric_scale=0)
        == "NUMBER(38,0)"
    )


def test_format_data_type_renders_binary_with_length() -> None:
    assert (
        format_data_type("BINARY", character_maximum_length=8388608)
        == "BINARY(8388608)"
    )


@pytest.mark.parametrize(
    "data_type", ["TIMESTAMP_LTZ", "TIMESTAMP_NTZ", "TIMESTAMP_TZ", "TIME"]
)
def test_format_data_type_appends_scale_to_timestamp_and_time_variants(
    data_type,
) -> None:
    assert format_data_type(data_type, datetime_precision=9) == f"{data_type}(9)"


@pytest.mark.parametrize(
    "data_type",
    [
        "BOOLEAN",
        "DATE",
        "VARIANT",
        "OBJECT",
        "ARRAY",
        "GEOGRAPHY",
        "GEOMETRY",
        "REAL",
        "UNKNOWN",
    ],
)
def test_format_data_type_leaves_categories_without_precision_unchanged(
    data_type,
) -> None:
    assert format_data_type(data_type) == data_type


def test_format_data_type_omits_parens_when_length_is_missing() -> None:
    assert format_data_type("TEXT") == "VARCHAR"


def test_format_data_type_omits_parens_when_scale_is_missing_for_fixed() -> None:
    """FIXED always reports both precision and scale together; a partial
    result would produce a malformed NUMBER(38) rather than the real type."""
    assert format_data_type("FIXED", numeric_precision=38) == "NUMBER"


def test_format_data_type_omits_parens_when_datetime_precision_is_missing() -> None:
    assert format_data_type("TIMESTAMP_LTZ") == "TIMESTAMP_LTZ"


def test_truncate_middle_keeps_both_ends() -> None:
    """Object names share long prefixes and long suffixes; cutting from one
    end alone would collapse distinct names to the same string."""
    assert truncate_middle("STG_CUSTOMER_ORDERS_DAILY", 11) == "STG_C…DAILY"


def test_truncate_middle_leaves_short_text_alone() -> None:
    assert truncate_middle("ORDERS", 20) == "ORDERS"


def test_truncate_middle_handles_widths_too_small_for_an_ellipsis() -> None:
    assert truncate_middle("ORDERS", 2) == "OR"
    assert truncate_middle("ORDERS", 0) == ""
