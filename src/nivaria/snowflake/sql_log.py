# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Logs every SQL statement fired against Snowflake, before it is sent.

Logged *before* `cursor.execute()` is called, not after: `cursor.query` (the
public attribute holding the resolved statement) is only set once `execute()`
has already returned, which is useless for "what was the connection doing
when it hung". `Cursor._preprocess_pyformat_query` is what the connector
itself calls, synchronously, before the network round-trip -- reusing it
means the logged text is guaranteed to match what is actually sent, with no
need to re-implement pyformat substitution here.

Deliberately does *not* go through `Connection._format_query_for_log`: besides
masking secrets, it also truncates to `Connection.log_max_query_length` --
80 characters by default, sized for the connector's own terse internal debug
stream, not for "show me the whole statement", which is the entire point
here. The masking half is not reproduced either: it exists for statements
that can embed cloud storage credentials (`COPY`/`CREATE STAGE
CREDENTIALS=(...)`, `PUT`/`GET`), and `queries.py` -- the only place any SQL
text is built -- never constructs any of those; every statement here is a
SHOW, an INFORMATION_SCHEMA/ACCOUNT_USAGE SELECT, or a plain `USE ROLE`/`USE
WAREHOUSE`. Reaching into `Connection._format_query_for_log` for masking that
can't ever trigger would also mean importing `snowflake.connector` from a
module `gateway.py`/`pool.py` pull in unconditionally -- exactly what
`tests/fakes.py` exists to keep out of every test.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


def log_statement(
    cursor: Any, statement: str, params: tuple[Any, ...] | None = None
) -> None:
    if not _log.isEnabledFor(logging.DEBUG):
        return
    try:
        resolved = (
            cursor._preprocess_pyformat_query(statement, params)
            if params
            else statement
        )
        formatted = " ".join(line.strip() for line in resolved.split("\n"))
    except Exception:  # noqa: BLE001 -- logging must never break a real query
        formatted = statement
    _log.debug(formatted)
