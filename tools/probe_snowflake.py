# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""One-off probe against a real Snowflake account.

Self-contained: this file imports nothing from nivaria, only
`snowflake-connector-python`. Copy this one file to the machine that can reach
your account and run it there.

It answers the two things nivaria's design assumes but cannot verify offline:

1. Does `SHOW <objects> ... LIMIT n [FROM 'name']` keyset paging work, and does
   it page *correctly* -- no skipped or repeated rows? A silent correctness bug
   here would be worse than an outright syntax error.
2. Which columns does each SHOW actually return? nivaria's cache mirrors these
   result sets column for column, and those column lists were written from
   documentation rather than observation.

Nothing is stored. Credentials come from arguments or an interactive prompt and
live only in this process.

    python probe_snowflake.py --account ORG-ACCT --user you@corp.com \
        --authenticator externalbrowser --role YOUR_ROLE \
        --database SOME_DB --out probe.txt

`--schema` is optional: given a database, the probe picks the schema with the
most tables by itself, which is the one worth paging through.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from collections import Counter

import snowflake.connector

SHOW_SCOPES = ("DATABASES", "SCHEMAS", "TABLES", "VIEWS", "PIPES", "STAGES", "TASKS")

#: Where every line of the report goes, so an air-gapped run can be carried
#: back as a file rather than retyped from a console.
_sink: list[str] = []
_out_path: str | None = None


def say(line: str = "") -> None:
    print(line)
    _sink.append(line)


def flush() -> None:
    if _out_path is None:
        return
    try:
        with open(_out_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(_sink) + "\n")
        print(f"\nReport written to {_out_path}")
    except OSError as error:
        print(f"\nCould not write {_out_path}: {error}")


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run(cursor, statement: str) -> tuple[list[dict], list[str], float]:
    started = time.time()
    cursor.execute(statement)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    return rows, columns, time.time() - started


def scope_clause(what: str, database: str | None, schema: str | None) -> str | None:
    if what == "DATABASES":
        return ""
    if what == "SCHEMAS":
        return f' IN DATABASE "{database}"' if database else None
    if not (database and schema):
        return None
    return f' IN SCHEMA "{database}"."{schema}"'


def report_session(cursor) -> None:
    say("### session")
    try:
        rows, _, _ = run(
            cursor,
            "SELECT CURRENT_ACCOUNT() AS account, CURRENT_ROLE() AS role, "
            "CURRENT_WAREHOUSE() AS warehouse, CURRENT_VERSION() AS version",
        )
    # A probe reports what a statement does, whatever that turns out to be.
    except Exception as error:  # noqa: BLE001
        say(f"  FAILED -- {type(error).__name__}: {error}")
        return
    for key, value in rows[0].items():
        say(f"  {key.lower():16s}: {value}")
    if not rows[0].get("ROLE"):
        say("  !! no active role -- pass --role or most SHOWs will come back empty")


def pick_schema(cursor, database: str) -> str | None:
    """Choose the schema with the most tables.

    Paging correctness only shows up with enough rows to page through, and on a
    machine the author cannot reach, guessing wrong costs a whole extra trip.
    """
    say(f"\n### choosing a schema in {database}")
    try:
        rows, _, elapsed = run(
            cursor, f'SHOW TABLES IN DATABASE "{database}" LIMIT 10000'
        )
    except Exception as error:  # noqa: BLE001
        say(f"  SHOW TABLES IN DATABASE failed -- {type(error).__name__}: {error}")
        return None
    counts = Counter(str(row.get("schema_name")) for row in rows)
    if not counts:
        say("  no tables visible in this database")
        return None
    say(f"  {len(rows)} tables across {len(counts)} schemas in {elapsed:.2f}s")
    for name, count in counts.most_common(5):
        say(f"    {name:40s} {count:6d} tables")
    chosen = counts.most_common(1)[0][0]
    say(f"  chosen: {chosen}")
    return chosen


def probe(cursor, what: str, database: str | None, schema: str | None) -> None:
    clause = scope_clause(what, database, schema)
    if clause is None:
        say(f"\n### {what}: skipped (needs --database/--schema)")
        return

    say(f"\n### {what}")
    base = f"SHOW {what}{clause}"

    try:
        full, columns, elapsed = run(cursor, f"{base} LIMIT 10000")
    except Exception as error:  # noqa: BLE001
        say(f"  LIMIT      : FAILED -- {type(error).__name__}: {error}")
        return
    say(f"  LIMIT      : ok, {len(full)} rows in {elapsed:.2f}s")
    say(f"  columns    : {', '.join(columns)}")

    if not full:
        say("  FROM       : skipped (no rows to page through)")
        return

    names = [str(row.get("name")) for row in full]
    try:
        run(cursor, f"{base} LIMIT 1 FROM {quote_literal(names[0])}")
    except Exception as error:  # noqa: BLE001
        say(f"  FROM       : FAILED -- {type(error).__name__}: {error}")
        say("  --> this object type needs the unpaged fallback")
        return
    say("  FROM       : accepted")

    # The real test: page through in small pages and compare with the full
    # list. Accepting the syntax is not the same as paging correctly.
    page_size = max(1, min(5, len(full) // 2 or 1))
    paged: list[str] = []
    cursor_name: str | None = None
    pages = 0
    started = time.time()
    while pages < 500:
        statement = f"{base} LIMIT {page_size}"
        if cursor_name is not None:
            statement += f" FROM {quote_literal(cursor_name)}"
        rows, _, _ = run(cursor, statement)
        pages += 1
        if not rows:
            break
        paged.extend(str(row.get("name")) for row in rows)
        if len(rows) < page_size:
            break
        cursor_name = str(rows[-1].get("name"))
    elapsed = time.time() - started

    if paged == names:
        say(f"  paging     : CORRECT ({pages} pages of {page_size}, {elapsed:.2f}s)")
        return

    say(f"  paging     : WRONG -- {len(paged)} rows paged vs {len(names)} unpaged")
    say(f"    duplicates : {len(paged) - len(set(paged))}")
    say(f"    missing    : {sorted(set(names) - set(paged))[:10]}")
    say(f"    unexpected : {sorted(set(paged) - set(names))[:10]}")
    if sorted(paged) == sorted(names):
        say("    (same rows, different order -- SHOW is not ordered by name)")
    say("  --> keyset paging is unusable for this object type")


def main() -> int:
    global _out_path

    parser = argparse.ArgumentParser(
        description="Probe a Snowflake account for SHOW paging support and columns.",
    )
    parser.add_argument("--account", required=True, help="e.g. ORG-ACCOUNT")
    parser.add_argument("--user", required=True)
    parser.add_argument(
        "--authenticator",
        default="externalbrowser",
        choices=["externalbrowser", "username_password_mfa"],
    )
    parser.add_argument(
        "--role", help="strongly recommended: without it most SHOWs are empty"
    )
    parser.add_argument("--warehouse")
    parser.add_argument("--database", help="a database to probe SCHEMAS/TABLES in")
    parser.add_argument(
        "--schema", help="a schema to probe TABLES/VIEWS/...; auto-picked if omitted"
    )
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args()
    _out_path = args.out

    kwargs = {
        "account": args.account,
        "user": args.user,
        "authenticator": args.authenticator,
        "client_session_keep_alive": True,
        "client_store_temporary_credential": True,
        "client_request_mfa_token": True,
    }
    if args.role:
        kwargs["role"] = args.role
    if args.warehouse:
        kwargs["warehouse"] = args.warehouse
    if args.authenticator == "username_password_mfa":
        kwargs["password"] = getpass.getpass("Snowflake password (not stored): ")

    say(f"nivaria probe -- {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say(f"connecting as {args.user} via {args.authenticator} ...")
    started = time.time()
    try:
        connection = snowflake.connector.connect(**kwargs)
    except Exception as error:  # noqa: BLE001
        say(f"CONNECT FAILED -- {type(error).__name__}: {error}")
        flush()
        return 1
    say(f"connected in {time.time() - started:.1f}s")

    cursor = connection.cursor()
    try:
        report_session(cursor)
        schema = args.schema
        if schema is None and args.database:
            schema = pick_schema(cursor, args.database)
        for what in SHOW_SCOPES:
            probe(cursor, what, args.database, schema)
    finally:
        cursor.close()
        connection.close()

    say("\ndone -- paste this report back")
    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
