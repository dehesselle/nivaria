"""One-off probe against a real Snowflake account.

Answers the two things snowcone's design assumes but cannot verify offline:

1. Does `SHOW <objects> ... LIMIT n [FROM 'name']` keyset paging work, and does
   it page *correctly* -- no skipped or repeated rows? A silent correctness bug
   here would be worse than an outright syntax error.
2. Which columns does each SHOW actually return? The cache mirror tables were
   written from documentation and need aligning with reality.

Nothing is stored. Credentials come from arguments or an interactive prompt and
live only in this process.

    python tools/probe_snowflake.py --account ORG-ACCT --user you@corp.com \
        --authenticator externalbrowser --database MYDB --schema PUBLIC
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time

import snowflake.connector

SHOW_SCOPES = ("DATABASES", "SCHEMAS", "TABLES", "VIEWS", "PIPES", "STAGES", "TASKS")


def scope_clause(what: str, database: str | None, schema: str | None) -> str | None:
    if what == "DATABASES":
        return ""
    if what == "SCHEMAS":
        return f' IN DATABASE "{database}"' if database else None
    if not (database and schema):
        return None
    return f' IN SCHEMA "{database}"."{schema}"'


def run(cursor, statement: str) -> tuple[list[dict], list[str], float]:
    started = time.time()
    cursor.execute(statement)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    return rows, columns, time.time() - started


def probe(cursor, what: str, database: str | None, schema: str | None) -> None:
    clause = scope_clause(what, database, schema)
    if clause is None:
        print(f"\n### {what}: skipped (needs --database/--schema)")
        return

    print(f"\n### {what}")
    base = f"SHOW {what}{clause}"

    try:
        full, columns, elapsed = run(cursor, f"{base} LIMIT 10000")
    # A probe reports what a statement does, whatever that turns out to be.
    except Exception as error:  # noqa: BLE001
        print(f"  LIMIT           : FAILED -- {type(error).__name__}: {error}")
        return
    print(f"  LIMIT           : ok, {len(full)} rows in {elapsed:.2f}s")
    print(f"  columns         : {', '.join(columns)}")

    if not full:
        print("  FROM            : skipped (no rows to page through)")
        return

    names = [row.get("name") for row in full]
    try:
        _, _, _ = run(
            cursor,
            f"{base} LIMIT 1 FROM '{str(names[0]).replace(chr(39), chr(39) * 2)}'",
        )
    except Exception as error:  # noqa: BLE001 -- see above
        print(f"  FROM            : FAILED -- {type(error).__name__}: {error}")
        print("  --> fall back to per-schema fanout for this object kind")
        return
    print("  FROM            : accepted")

    # The real test: page through in small pages and compare with the full list.
    page_size = max(1, min(5, len(full) // 2 or 1))
    paged: list[str] = []
    cursor_name: str | None = None
    pages = 0
    started = time.time()
    while pages < 200:
        statement = f"{base} LIMIT {page_size}"
        if cursor_name is not None:
            statement += f" FROM '{cursor_name.replace(chr(39), chr(39) * 2)}'"
        rows, _, _ = run(cursor, statement)
        pages += 1
        if not rows:
            break
        paged.extend(str(r.get("name")) for r in rows)
        if len(rows) < page_size:
            break
        cursor_name = str(rows[-1].get("name"))
    elapsed = time.time() - started

    expected = [str(n) for n in names]
    if paged == expected:
        print(
            f"  paging          : CORRECT ({pages} pages of {page_size}, {elapsed:.2f}s)"
        )
    else:
        print(
            f"  paging          : WRONG -- {len(paged)} paged vs {len(expected)} full"
        )
        print(f"    duplicates    : {len(paged) - len(set(paged))}")
        print(f"    missing       : {sorted(set(expected) - set(paged))[:10]}")
        print(f"    unexpected    : {sorted(set(paged) - set(expected))[:10]}")
        print("  --> keyset paging is unusable; fall back to per-schema fanout")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument(
        "--authenticator",
        default="externalbrowser",
        choices=["externalbrowser", "username_password_mfa"],
    )
    parser.add_argument("--role")
    parser.add_argument("--warehouse")
    parser.add_argument("--database", help="a database to probe SCHEMAS in")
    parser.add_argument("--schema", help="a schema to probe TABLES/VIEWS/... in")
    args = parser.parse_args()

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

    print(f"Connecting as {args.user} via {args.authenticator} ...")
    started = time.time()
    connection = snowflake.connector.connect(**kwargs)
    print(f"Connected in {time.time() - started:.1f}s")

    cursor = connection.cursor()
    try:
        for what in SHOW_SCOPES:
            probe(cursor, what, args.database, args.schema)
    finally:
        cursor.close()
        connection.close()
    print("\nDone. Paste this output back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
