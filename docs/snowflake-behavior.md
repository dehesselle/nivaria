# Observed Snowflake behaviour

This document records what live Snowflake accounts were observed to do where
nivaria's design depends on it, with when and against what each was checked.

The rules derived from these findings are in [DESIGN.md](DESIGN.md); this file
holds the evidence. Timings are in [measurements.md](measurements.md).

## Accounts probed

- **The reference account** (Snowflake 10.31.103): where the `SHOW` column
  sets were first verified. It runs with no warehouse at all, and everything
  `SHOW`-sourced works.
- **The sandbox** (Snowflake 10.32.102): its
  `RENETEST1`/`2`/`3` databases carry 614–657 tables and pipes each, built
  for scale checks with `tools/gen_test_ddl.py`. Test objects created there
  for verification are noted below.
- **A production account**: probed by handing queries to the user to run and
  relay counts back from, so no session acted on production directly.

## Verified column sets

Every `SHOW`-sourced mirror in `schema.sql` was verified against a live
account (see that file's header).

The `INFORMATION_SCHEMA`/`ACCOUNT_USAGE` mirrors' column names were first
written from Snowflake's documentation. On 2026-09-09 every one of those
column lists was run against the sandbox and resolved without error:
`information_schema_tables`, `information_schema_views`,
`account_usage_copy_history`, `account_usage_copy_history_detail`,
`information_schema_copy_history`, `account_usage_task_history`,
`account_usage_task_history_detail`, `information_schema_task_history`,
`information_schema_tag_references` and `show_grants`. A wrong name would
surface as that one scope rendering blank, never a crash.

A second round the same day confirmed *values*, partly on production and
partly by creating test data on the sandbox:

- `information_schema_tables`/`information_schema_views`: `table_stats`
  (`TABLE_TYPE = 'BASE TABLE'`) returned real `LAST_DDL`/`LAST_ALTERED`
  timestamps for 27 tables on the sandbox. `view_stats` is the identical
  statement bound to `'VIEW'`; the sandbox held no user-created view, but
  production returned 37 views, all with non-null `LAST_DDL` and
  `LAST_ALTERED`. **Confirmed.**
- `show_materialized_views`: confirmed by creating one on the sandbox (500
  rows inserted into `RENETEST1.ABIDING_NOUGAT.ADAMANT_RELISH`, then `CREATE
  MATERIALIZED VIEW ... AS SELECT ... FROM` that table). The `SHOW
  MATERIALIZED VIEWS` row carried `rows = 500` and `bytes = 11264`, plus a
  correctly resolved `source_database_name`/`source_schema_name`/
  `source_table_name` and populated `refreshed_on`/`compacted_on`.
  **Confirmed.** The view was later removed; the sandbox has no materialized
  views as of 2026-10-14.
- `account_usage_copy_history`(`_detail`)/`information_schema_copy_history`:
  **confirmed**, including the `QUALIFY` reduction's row collapsing. The
  numbers are in [measurements.md](measurements.md#history-volume).
- `account_usage_task_history`(`_detail`): **confirmed** on production — 7
  task runs from 1 task over a 7-day window. `information_schema_task_history`
  is **confirmed** by a task created on the sandbox
  (`RENETEST1.ABIDING_NOUGAT.CLAUDE_VERIFY_TASK`, `AS SELECT 1`, three manual
  runs, left in place): `SCHEDULED_TIME`, `COMPLETED_TIME`, `STATE` and, on
  runs that failed because the sandbox's `SYSADMIN` lacks `USAGE` on warehouse
  `WH`, `ERROR_CODE`/`ERROR_MESSAGE` all came back non-null.
  `QUERY_ID`/`QUERY_START_TIME` stayed `NULL`, consistent with the task
  failing before a query was submitted; not chased further, since the grant
  needs `ACCOUNTADMIN`.
- `information_schema_tag_references`: **confirmed** on production — 3 tag
  references covering 3 tag names.
- `granted_by_role_type` on `SHOW GRANTS` exists and reads `'ROLE'` on real
  `SHOW GRANTS ON SCHEMA`/`ON DATABASE` output.
- `information_schema_copy_history`'s `PIPE_NAME` output column is
  bare/unqualified, and its `TABLE_NAME` argument accepts a fully qualified
  `catalog.schema.table` string: the statement compiled and ran.

## Warehouse requirements

Tested directly with no warehouse active: `INFORMATION_SCHEMA.TASK_HISTORY`,
`COPY_HISTORY`, `TAG_REFERENCES`, `table_stats` and `view_stats` all fail with
`No active warehouse selected in the current session`. `SHOW` commands need
none.

## Keyset paging

`LIMIT n FROM 'name'` was verified correct — same rows, same order as an
unpaged fetch — for databases, schemas, tables and views (the latter on
2026-09-09, against a schema with 63 views), at both `IN SCHEMA` and `IN
DATABASE` scope. It is **unverified for tasks**: no account probed so far has
had enough of them to page through.

**`LIMIT`/`FROM` is a no-op for `SHOW PIPES`, `SHOW COLUMNS` and `SHOW
STAGES`, at both `IN SCHEMA` and `IN DATABASE` scope.**

- Sandbox, 2026-09-09: `SHOW PIPES IN SCHEMA <schema-with-27-pipes> LIMIT 5`
  returned all 27 rows; `SHOW PIPES IN DATABASE <db-with-614-pipes> LIMIT 300`
  returned all 614; the identical query repeated with `FROM '<last pipe
  name>'` returned the same 614 again, not the next page. `SHOW COLUMNS IN
  SCHEMA`/`IN DATABASE` behaved identically. Tables and views, tested the same
  way in the same session, honoured `LIMIT` at both scopes, so this is
  specific to a subset of object types.
- Production, the same day: `SHOW PIPES IN SCHEMA ... LIMIT 10` returned all
  395 pipes in the schema; `SHOW COLUMNS IN SCHEMA ... LIMIT 10` returned all
  6655 columns; `SHOW STAGES IN SCHEMA ... LIMIT 10` returned every stage.

Neither account had a schema with more than 1000 pipes, so the continuation
loop this implies (see [DESIGN.md](DESIGN.md#why-a-paging-no-op-is-a-hang-not-a-slow-fetch))
was never observed firing in the wild; lowering `PAGE_SIZE` below a schema's
pipe count reproduces it.

## SHOW row counts

Snowflake's "SHOW caps at 10k rows regardless of `LIMIT`" does not hold
universally. `SHOW COLUMNS IN DATABASE` against a sandbox database with 614
tables across 25 schemas returned **11 511 rows** in one response (2026-09-09).
The result shape matched `map_show_columns`'s assumptions exactly
(`TEXT`/`FIXED`/`TIMESTAMP_NTZ`/`VARIANT` blobs all parsed as expected).

## SHOW COLUMNS types

A live comparison of `SHOW COLUMNS`, `INFORMATION_SCHEMA.COLUMNS` and `DESC
TABLE` found only one difference in type names: SHOW's JSON reports `FIXED`
and `REAL` where the others say `NUMBER` and `FLOAT`. `DESC TABLE`'s rendering
of 51 declared types is kept in `tests/data/show_column_types.json`.

## SHOW GRANTS

`SHOW GRANTS ON TABLE` was checked on 2026-10-14 against the sandbox, through
`PoolGateway` and a throwaway cache: it returns exactly the same nine columns
as `ON SCHEMA`, with `granted_on = 'TABLE'` and `name` the table's fully
qualified name. `grant_option` arrives as the strings `'true'`/`'false'`. The
owner's `OWNERSHIP` row reads `grant_option = 'true'`. Exercised by granting
`SELECT ... WITH GRANT OPTION` and `INSERT` on
`RENETEST1.BLUE_THORN.CAPABLE_OPTION` to `PUBLIC`, then revoking both.

For a materialized view, `SHOW GRANTS ON VIEW` and `ON MATERIALIZED VIEW`
returned the same rows on the sandbox (2026-10-14), with `granted_on` reading
`MATERIALIZED_VIEW`.

## Tag references

`INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS` reports a schema- or
database-level tag against every column of every table underneath it too,
with `APPLY_METHOD = 'INHERITED'` (checked 2026-09-14). Without filtering
those rows, every column shows its schema's tags as its own.

## Copy history

`ACCOUNT_USAGE.COPY_HISTORY` and `INFORMATION_SCHEMA.COPY_HISTORY` stamp
`LAST_LOAD_TIME` independently: on production the same load carried
`11:11:24.961` in one and `11:11:24.926` in the other. Comparing all shared
columns for every overlapping load found `LAST_LOAD_TIME` the only one that
differed, and `PIPE_RECEIVED_TIME` equal and non-NULL in every pair.

## Task history

An unqualified `INFORMATION_SCHEMA.TASK_HISTORY(...)` resolves against the
session's current database. With none set, it failed on a live account with
"invalid identifier", regardless of the `DATABASE_NAME` argument.

## ACCOUNT_USAGE lag

The lag figures (~2 h for copy history, ~45 min for task history) are
Snowflake's published numbers, and nivaria does not track them further: the
lag is neither guaranteed nor reproducible, so a measurement at one moment
would be no more trustworthy than Snowflake's "up to" figure. One production
comparison showed `INFORMATION_SCHEMA.COPY_HISTORY` seeing a load 2 minutes
28 seconds before `ACCOUNT_USAGE.COPY_HISTORY` did, consistent with the
published figures.

## Query timeout

`cursor.execute(..., timeout=n)` cancels the query server-side. On the sandbox
on 2026-10-14, a 1 s timeout on `SELECT SYSTEM$WAIT(5)` raised `000604
(57014): SQL execution was cancelled by the client due to a timeout`, once,
and the connection stayed in the pool and answered the next query.

## externalbrowser login

The installed driver's `AuthByWebBrowser` waits on a raw `select.select()` with
no timeout argument anywhere in that code path, confirmed by reading it: no
connect kwarg reaches it, and `socket.setdefaulttimeout()` would not either,
since `select.select()` ignores a socket's `.settimeout()`. An abandoned
browser tab therefore blocks the connection attempt indefinitely.
