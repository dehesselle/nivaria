# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""Generate DDL to populate a near-empty Snowflake sandbox with test objects.

Self-contained: this file imports nothing from nivaria. It writes a single
`.sql` file of `CREATE OR REPLACE` statements -- databases, schemas, tables
with a mix of column types, and one pipe per table -- so a sandbox account
with practically nothing in it becomes a realistic object tree to browse.

Every pipe's COPY INTO reads from its own table's implicit table stage
(`@%table_name`), which always exists with no separate STAGE object. The
pipe is never meant to run -- there's no file to ingest -- it only needs to
exist.

Every statement uses `CREATE OR REPLACE`, so the output is safe to re-run.

    python tools/gen_test_ddl.py --seed 1 --out tools/renetest_ddl.sql
"""

from __future__ import annotations

import argparse
import random
import time

from wonderwords import RandomWord

VARCHAR_LENGTHS = [16, 32, 50, 64, 100, 128, 200, 255, 500, 1000, 4000]
NUMBER_PRECISIONS = [(38, 0), (18, 0), (10, 0), (9, 2), (12, 2), (18, 4)]

_word_generator = RandomWord()


def _word(part_of_speech: str) -> str:
    # wonderwords' lists include a few hyphenated/multi-word entries
    # ("e-mail", "ad hoc") that aren't valid identifier fragments.
    return _word_generator.word(
        include_parts_of_speech=[part_of_speech],
        word_min_length=3,
        word_max_length=8,
        regex=r"^[a-z]+$",
    )


def unique_name(existing: set[str]) -> str:
    """A short adjective_noun identifier, unique within `existing`.

    Falls back to a numeric suffix if the word pool is ever exhausted --
    not expected at the volumes this script generates, but cheap insurance
    against an infinite loop.
    """
    for _ in range(50):
        candidate = f"{_word('adjectives')}_{_word('nouns')}"
        if candidate not in existing:
            existing.add(candidate)
            return candidate
    candidate = f"{candidate}_{len(existing)}"
    existing.add(candidate)
    return candidate


def random_column_type() -> str:
    if random.random() < 0.5:
        length = random.choice(VARCHAR_LENGTHS)
        return f"VARCHAR({length})"
    precision, scale = random.choice(NUMBER_PRECISIONS)
    return f"NUMBER({precision},{scale})"


def make_columns(count: int) -> list[tuple[str, str]]:
    """`count` (name, type) pairs, guaranteeing one TIMESTAMP_NTZ and one VARIANT."""
    names: set[str] = set()
    columns = [(unique_name(names), "TIMESTAMP_NTZ")]
    if count > 1:
        columns.append((unique_name(names), "VARIANT"))
    while len(columns) < count:
        columns.append((unique_name(names), random_column_type()))
    random.shuffle(columns)
    return columns


def table_ddl(
    database: str, schema: str, table: str, columns: list[tuple[str, str]]
) -> str:
    column_lines = ",\n".join(f"    {name} {type_}" for name, type_ in columns)
    return f"CREATE OR REPLACE TABLE {database}.{schema}.{table} (\n{column_lines}\n);"


def pipe_ddl(database: str, schema: str, table: str) -> str:
    qualified = f"{database}.{schema}.{table}"
    return (
        f"CREATE OR REPLACE PIPE {qualified}\n"
        f"  AUTO_INGEST = FALSE\n"
        f"AS\n"
        f"COPY INTO {qualified}\n"
        f"FROM @%{table}\n"
        f"FILE_FORMAT = (TYPE = CSV);"
    )


def generate(
    databases: list[str],
    schema_range: tuple[int, int],
    table_range: tuple[int, int],
    column_range: tuple[int, int],
) -> tuple[list[str], dict[str, int]]:
    statements: list[str] = []
    counts = {"databases": 0, "schemas": 0, "tables": 0, "columns": 0, "pipes": 0}

    for database in databases:
        statements.append(f"\n-- ===== database {database} =====")
        statements.append(f"CREATE OR REPLACE DATABASE {database};")
        counts["databases"] += 1

        schema_names: set[str] = set()
        for _ in range(random.randint(*schema_range)):
            schema = unique_name(schema_names)
            statements.append(f"\n-- ---- schema {database}.{schema} ----")
            statements.append(f"CREATE OR REPLACE SCHEMA {database}.{schema};")
            counts["schemas"] += 1

            table_names: set[str] = set()
            for _ in range(random.randint(*table_range)):
                table = unique_name(table_names)
                columns = make_columns(random.randint(*column_range))
                statements.append(table_ddl(database, schema, table, columns))
                statements.append(pipe_ddl(database, schema, table))
                counts["tables"] += 1
                counts["columns"] += len(columns)
                counts["pipes"] += 1

    return statements, counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate CREATE OR REPLACE DDL to populate a Snowflake sandbox.",
    )
    parser.add_argument(
        "--databases",
        default="renetest1,renetest2,renetest3",
        help="comma-separated database names",
    )
    parser.add_argument("--min-schemas", type=int, default=20)
    parser.add_argument("--max-schemas", type=int, default=50)
    parser.add_argument("--min-tables", type=int, default=20)
    parser.add_argument("--max-tables", type=int, default=30)
    parser.add_argument("--min-columns", type=int, default=5)
    parser.add_argument("--max-columns", type=int, default=30)
    parser.add_argument("--seed", type=int, help="random seed, for reproducible output")
    parser.add_argument("--out", default="tools/renetest_ddl.sql")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    databases = [name.strip() for name in args.databases.split(",") if name.strip()]

    statements, counts = generate(
        databases,
        (args.min_schemas, args.max_schemas),
        (args.min_tables, args.max_tables),
        (args.min_columns, args.max_columns),
    )

    header = (
        f"-- Generated by tools/gen_test_ddl.py at "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"-- seed={args.seed}\n"
        f"-- Safe to re-run: every statement is CREATE OR REPLACE.\n"
    )

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(header)
        handle.write("\n".join(statements) + "\n")

    print(f"wrote {args.out}")
    print(
        f"  {counts['databases']} databases, {counts['schemas']} schemas, "
        f"{counts['tables']} tables, {counts['columns']} columns, "
        f"{counts['pipes']} pipes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
