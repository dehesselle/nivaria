# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""copy_target: a pipe's target table from the start of its definition."""

from __future__ import annotations

import pytest

from nivaria.pipe_definition import copy_target


def test_an_unquoted_fully_qualified_target_is_stored_upper_cased() -> None:
    assert copy_target("COPY INTO mydb.raw.orders FROM @s") == ("MYDB", "RAW", "ORDERS")


def test_quoted_parts_are_kept_as_written() -> None:
    assert copy_target('copy into "MyDb".raw."t ""x"" y" from @s') == (
        "MyDb",
        "RAW",
        't "x" y',
    )


def test_the_sandbox_shape_with_crlf_line_ends() -> None:
    definition = (
        "COPY INTO renetest1.blue_thorn.capable_option\r\n"
        "FROM @%capable_option\r\n"
        "FILE_FORMAT = (TYPE = CSV)"
    )
    assert copy_target(definition) == ("RENETEST1", "BLUE_THORN", "CAPABLE_OPTION")


def test_comments_before_and_between_tokens_are_skipped() -> None:
    definition = "/* a */ COPY -- b\n INTO // c\n db /* d */ . s . t FROM @s"
    assert copy_target(definition) == ("DB", "S", "T")


def test_a_column_list_may_follow_the_target_directly() -> None:
    assert copy_target("COPY INTO db.s.t(a, b) FROM (SELECT $1, $2 FROM @s)") == (
        "DB",
        "S",
        "T",
    )


@pytest.mark.parametrize(
    "options",
    [
        "ON_ERROR = SKIP_FILE_10%",
        "LOAD_MODE = ADD_FILES_COPY",
        "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE INCLUDE_METADATA = (c = x)",
        "SOME_FUTURE_OPTION = 42",
    ],
)
def test_what_follows_the_target_does_not_matter(options: str) -> None:
    """A full SQL parser lost the target on an option it did not know."""
    assert copy_target(f"COPY INTO db.s.t FROM @s {options}") == ("DB", "S", "T")


@pytest.mark.parametrize(
    "definition",
    [
        # Resolved against the session's schema at CREATE PIPE time, which
        # the text does not record.
        "COPY INTO t FROM @s",
        "COPY INTO s.t FROM @s",
        'COPY INTO "probe lower" FROM @probe_stage',
        "COPY INTO a.b.c.d FROM @s",
        "COPY INTO IDENTIFIER('db.s.t') FROM @s",
        'COPY INTO db.s."unterminated FROM @s',
        "COPY INTO db.s.t@s",
        "COPYINTO db.s.t FROM @s",
        "INSERT INTO db.s.t VALUES (1)",
        "",
    ],
)
def test_anything_but_three_parts_after_copy_into_gives_none(definition: str) -> None:
    assert copy_target(definition) is None
