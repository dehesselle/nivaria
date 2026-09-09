from __future__ import annotations

from datetime import datetime

import pytest

from snowcone.format import (
    format_bytes,
    format_count,
    format_data_type,
    format_date,
    format_timestamp_full,
    format_timestamp_minutes,
    parse_timestamp,
)


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


def test_format_date_truncates_to_the_calendar_day() -> None:
    assert format_date("2026-01-02 13:45:07") == "2026-01-02"


def test_format_date_of_none_or_empty_is_blank() -> None:
    assert format_date(None) == ""
    assert format_date("") == ""


def test_format_date_falls_back_to_the_raw_value_when_it_does_not_parse() -> None:
    """A timestamp snowcone cannot parse is still a value worth showing, same
    convention as format_data_type's unparseable-type fallback."""
    assert format_date("not a timestamp") == "not a timestamp"


def test_format_timestamp_minutes_truncates_to_the_minute() -> None:
    assert format_timestamp_minutes("2026-01-02 13:45:07") == "2026-01-02 13:45"


def test_format_timestamp_minutes_of_none_or_empty_is_blank() -> None:
    assert format_timestamp_minutes(None) == ""
    assert format_timestamp_minutes("") == ""


def test_format_timestamp_minutes_falls_back_to_the_raw_value() -> None:
    assert format_timestamp_minutes("not a timestamp") == "not a timestamp"


def test_format_timestamp_full_keeps_seconds() -> None:
    value = "2026-01-02 13:45:07"
    expected = (
        datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    )
    assert format_timestamp_full(value) == expected.strip()


def test_format_timestamp_full_converts_an_aware_value_to_local_time() -> None:
    """A stored offset is a real instant, not a display preference -- the
    tooltip must show this machine's local time, not whatever offset
    Snowflake happened to report. Computed rather than hard-coded so the
    assertion holds regardless of which timezone the test runs in."""
    value = "2026-01-02 13:45:07+00:00"
    expected = (
        datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    )
    assert format_timestamp_full(value) == expected.strip()


def test_format_date_and_minutes_also_convert_an_aware_value_to_local_time() -> None:
    value = "2026-01-02 13:45:07+00:00"
    local = datetime.fromisoformat(value).astimezone()
    assert format_date(value) == local.strftime("%Y-%m-%d")
    assert format_timestamp_minutes(value) == local.strftime("%Y-%m-%d %H:%M")


def test_a_naive_values_wall_clock_numbers_are_left_alone() -> None:
    """No offset means `astimezone()` cannot know what zone the value was
    actually in, so per its own documented behaviour it presumes the naive
    value is already local -- the wall-clock numbers must stay exactly as
    stored rather than being silently reinterpreted as some other zone. A
    local UTC offset is still appended (see format_timestamp_full's
    docstring), so only the numbers before it are asserted here."""
    assert format_timestamp_full("2026-01-02 13:45:07").startswith(
        "2026-01-02 13:45:07"
    )


def test_format_timestamp_full_of_none_or_empty_is_blank() -> None:
    assert format_timestamp_full(None) == ""
    assert format_timestamp_full("") == ""


def test_format_timestamp_full_falls_back_to_the_raw_value() -> None:
    assert format_timestamp_full("not a timestamp") == "not a timestamp"


def test_parse_timestamp_matches_format_timestamp_fulls_own_parsing() -> None:
    value = "2026-01-02 13:45:07+00:00"
    assert parse_timestamp(value) == datetime.fromisoformat(value).astimezone()


def test_parse_timestamp_of_none_or_empty_is_none() -> None:
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None


def test_parse_timestamp_of_unparseable_text_is_none_not_a_raise() -> None:
    """Unlike the format_* functions, which fall back to the raw string for
    display, a comparison has nothing sensible to fall back to -- callers
    (e.g. picking the most recent of several rows) treat None as "loses
    every comparison" rather than crashing on one bad row."""
    assert parse_timestamp("not a timestamp") is None
