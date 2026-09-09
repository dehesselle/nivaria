"""Pure display formatting. No Textual, no I/O."""

from __future__ import annotations

from datetime import datetime


def format_count(count: int) -> str:
    """The parenthesised child count in a tree label, e.g. `(40)`."""
    return f"({count})"


_BYTE_UNITS = ("B", "K", "M", "G", "T", "P")


def format_bytes(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in _BYTE_UNITS:
        if value < 1024 or unit == _BYTE_UNITS[-1]:
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}{_BYTE_UNITS[-1]}"


_DDL_NAMES = {"TEXT": "VARCHAR", "FIXED": "NUMBER"}
_SCALE_ONLY = {"TIMESTAMP_LTZ", "TIMESTAMP_NTZ", "TIMESTAMP_TZ", "TIME"}


def format_data_type(
    data_type: str,
    *,
    character_maximum_length: int | None = None,
    numeric_precision: int | None = None,
    numeric_scale: int | None = None,
    datetime_precision: int | None = None,
) -> str:
    """A DDL-style type name, e.g. VARCHAR(16777216), NUMBER(38,0), TIMESTAMP_LTZ(9).

    SHOW COLUMNS reports a bare category with precision split into separate
    fields; this recombines them so the column list reads like a CREATE
    TABLE statement instead of Snowflake's internal category names.
    """
    if data_type == "FIXED":
        if numeric_precision is not None and numeric_scale is not None:
            return f"NUMBER({numeric_precision},{numeric_scale})"
        return "NUMBER"
    if data_type in ("TEXT", "BINARY"):
        name = _DDL_NAMES.get(data_type, data_type)
        if character_maximum_length is not None:
            return f"{name}({character_maximum_length})"
        return name
    if data_type in _SCALE_ONLY:
        if datetime_precision is not None:
            return f"{data_type}({datetime_precision})"
        return data_type
    return data_type


def _local(value: str) -> datetime:
    """Parse a stored timestamp and convert it to this machine's local time.

    Snowflake timestamps round-trip through sqlite with their original offset
    intact (an aware `datetime`'s default adapter includes it), so a value
    with an offset is a real instant and `astimezone()` converts it
    correctly. A naive value (no offset -- not expected from the sources this
    app reads today, but not verified either, see DESIGN.md's "Known gaps")
    is presumed already local per `datetime.astimezone`'s own documented
    behaviour, so it passes through with the same wall-clock numbers rather
    than being silently reinterpreted as some other zone.
    """
    return datetime.fromisoformat(value).astimezone()


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a stored timestamp into an aware local `datetime`, or None.

    Same parsing as `_local` (used by every `format_*` below), but returns
    None on absence/failure instead of raising -- for callers that need to
    *compare* timestamps (picking the most recent of several rows, or
    filtering to a display window) rather than render one.
    """
    if not value:
        return None
    try:
        return _local(value)
    except ValueError:
        return None


def format_date(value: str | None) -> str:
    """YYYY-MM-DD in local time, for a cell too narrow for the full timestamp.

    Falls back to the raw value when it doesn't parse, the same
    degrade-don't-fail convention `format_data_type` uses for an unparseable
    SHOW COLUMNS type -- a timestamp snowcone cannot format is still a
    timestamp worth showing.
    """
    if not value:
        return ""
    try:
        return _local(value).strftime("%Y-%m-%d")
    except ValueError:
        return value


def format_timestamp_minutes(value: str | None) -> str:
    """YYYY-MM-DD HH:MM in local time, same reasoning as `format_date`."""
    if not value:
        return ""
    try:
        return _local(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def format_timestamp_full(value: str | None) -> str:
    """The full-precision local timestamp, for a hover tooltip over a
    truncated cell.

    Always carries a local UTC offset, even for a naive input: `_local`
    returns a genuinely aware `datetime` either way (converted for an aware
    input, tagged with the local zone for a naive one), so `%z` is never
    blank here -- `.strip()` only guards a hypothetical empty offset, not one
    that's actually expected in practice.
    """
    if not value:
        return ""
    try:
        return _local(value).strftime("%Y-%m-%d %H:%M:%S %z").strip()
    except ValueError:
        return value
