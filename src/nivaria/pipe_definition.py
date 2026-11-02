# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""A pipe's target table, read off the start of its COPY definition.

Snowflake exposes the table a pipe loads into in no named field: SHOW PIPES,
INFORMATION_SCHEMA.PIPES and SYSTEM$PIPE_STATUS have none, and every source
that names it records a load, which a pipe that never loaded does not have
(see docs/snowflake-behavior.md "Pipe target table"). The definition is the
one place left.

Only the statement's start is read -- `COPY INTO` and the table name -- and
only a fully qualified name is accepted. A full SQL parser was rejected:
it parses the whole statement, so an option it does not know further on
loses the target too (sqlglot fell back to an unparsed command on
`ON_ERROR = SKIP_FILE_10%`, a documented value). An unqualified or
schema-qualified name is not resolved against the pipe's own schema: Snowflake
resolves it against the session's schema at CREATE PIPE time and keeps the
text as written, so the definition does not say which table it is.
"""

from __future__ import annotations

import re

#: Snowflake's unquoted identifier: a letter or underscore, then letters,
#: digits, underscores or dollar signs. Stored upper-cased.
_UNQUOTED = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
#: A quoted identifier, with `""` standing for one embedded quote. Stored as
#: written.
_QUOTED = re.compile(r'"((?:[^"]|"")*)"')
#: Whitespace and the three comment forms Snowflake accepts.
_GAP = re.compile(r"(?:\s+|--[^\n]*|//[^\n]*|/\*.*?\*/)*", re.DOTALL)


def copy_target(definition: str) -> tuple[str, str, str] | None:
    """The (database, schema, table) a pipe's COPY loads into, or None.

    None unless the definition starts with `COPY INTO` followed by exactly
    three identifier parts and then a gap, a column list or the end.
    """
    position = _skip(definition, 0)
    for keyword in ("COPY", "INTO"):
        match = _UNQUOTED.match(definition, position)
        if match is None or match.group().upper() != keyword:
            return None
        position = _skip(definition, match.end())
    parts: list[str] = []
    while True:
        part = _identifier(definition, position)
        if part is None:
            return None
        name, position = part
        parts.append(name)
        after = _skip(definition, position)
        if not definition.startswith(".", after):
            break
        position = _skip(definition, after + 1)
    if len(parts) != 3:
        return None
    if position < len(definition) and not (
        _skip(definition, position) > position or definition[position] == "("
    ):
        return None
    return parts[0], parts[1], parts[2]


def _skip(text: str, position: int) -> int:
    """Past any whitespace and comments at `position`."""
    match = _GAP.match(text, position)
    return match.end() if match is not None else position


def _identifier(text: str, position: int) -> tuple[str, int] | None:
    """One identifier at `position`, as stored, and where it ends."""
    quoted = _QUOTED.match(text, position)
    if quoted is not None:
        return quoted.group(1).replace('""', '"'), quoted.end()
    unquoted = _UNQUOTED.match(text, position)
    if unquoted is None:
        return None
    return unquoted.group().upper(), unquoted.end()
