"""Pure display formatting. No Textual, no I/O."""

from __future__ import annotations

_AGE_UNITS: tuple[tuple[int, str], ...] = (
    (86400, "d"),
    (3600, "h"),
    (60, "m"),
)


def format_age(seconds: float | None) -> str:
    """A compact cache age: `never`, `12s`, `5m`, `3h`, `2d`.

    Coarse on purpose -- this sits in a status bar next to a per-second
    countdown, and a second changing figure there is just noise.
    """
    if seconds is None:
        return "never"
    seconds = max(0.0, seconds)
    for size, suffix in _AGE_UNITS:
        if seconds >= size:
            return f"{int(seconds // size)}{suffix}"
    return f"{int(seconds)}s"


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


def truncate_middle(text: str, width: int) -> str:
    """Shorten to `width`, keeping both ends.

    Object names share long prefixes (STG_/DIM_) *and* long suffixes
    (_V2, _DAILY), so cutting from either end alone loses the distinguishing
    part; an ellipsis in the middle keeps both.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    keep = width - 1
    head = (keep + 1) // 2
    tail = keep - head
    return f"{text[:head]}…{text[len(text) - tail :]}" if tail else f"{text[:head]}…"
