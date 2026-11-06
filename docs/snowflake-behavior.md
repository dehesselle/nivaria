# Observed Snowflake behaviour

This document records what live Snowflake accounts were observed to do where
nivaria's design depends on it, with when and against what each was checked.

The rules derived from these findings are in [DESIGN.md](DESIGN.md); this file
holds the evidence. Timings are in [measurements.md](measurements.md).

## Accounts probed

- **The reference account** (Snowflake 10.31.103): where the `SHOW` column
  sets were first verified. It runs with no warehouse at all, and everything
  `SHOW`-sourced works.
- **The sandbox** (Snowflake 10.32.102, 10.38.101
  on 2026-10-24): its `RENETEST1`/`2`/`3` databases carry 587–657 tables and
  pipes each, built for scale checks with `tools/gen_test_ddl.py`. The
  session role is `SYSADMIN`, with secondary roles `ALL` (`ACCOUNTADMIN`
  among them). Test objects created there for verification are noted below.
  The 2026-10-24 round created everything in `RENETEST1.NIVARIA_PROBE_A`/`_B`
  and dropped both schemas afterwards.
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
  is **confirmed** by tasks created on the sandbox: `SCHEDULED_TIME`,
  `COMPLETED_TIME`, `STATE` and, on failed runs, `ERROR_CODE`/`ERROR_MESSAGE`
  all came back non-null. See [Task history](#task-history) for when
  `QUERY_ID` is `NULL`.
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

That error is errno 606, printed as `000606 (57P03)`, and the session that
reports it stays usable. On the sandbox on 2026-11-06, connecting as `SYSADMIN`
without a warehouse left `CURRENT_WAREHOUSE()` `NULL`. On one connection, every
statement behind table and view stats, tags, the data preview, and pipe and task
activity and history answered with errno 606, the `ACCOUNT_USAGE` ones included,
and `SHOW ROLES` then succeeded on that same connection. Materialized view stats
come from `SHOW`, so they need no warehouse.

Context functions need neither a warehouse nor a privilege: on the sandbox on
2026-11-02, `SELECT CURRENT_WAREHOUSE(), CURRENT_ORGANIZATION_NAME(),
CURRENT_ACCOUNT_NAME()` succeeded as `PUBLIC` with `CURRENT_WAREHOUSE()`
returning `NULL`.

## Missing objects

What a statement naming something that does not exist fails with depends on the
statement, not on the object. On the sandbox on 2026-11-06, as `SYSADMIN`:

| errno | Message | Statements |
| --- | --- | --- |
| 2003 | `<Kind> '<name>' does not exist or not authorized.` | `SHOW PIPES`, `SHOW TASKS` and `SHOW MATERIALIZED VIEWS IN SCHEMA`; `SHOW GRANTS ON SCHEMA`, `ON DATABASE` and `ON TABLE`; `SHOW COLUMNS IN TABLE`; `SELECT` |
| 2043 | `Object does not exist, or operation cannot be performed.` | `SHOW TABLES`, `SHOW VIEWS` and `SHOW STAGES IN SCHEMA`; `SHOW SCHEMAS IN DATABASE`; `SHOW TABLES IN DATABASE` |

The SQL state does not separate them either: a missing schema or database is
`02000` under both numbers, a missing table `42S02`.

## Keyset paging

`LIMIT n FROM 'name'` was verified correct — same rows, same order as an
unpaged fetch — for databases, schemas, tables and views (the latter on
2026-09-09, against a schema with 63 views), at both `IN SCHEMA` and `IN
DATABASE` scope.

**`SHOW TASKS` honours `LIMIT`, but its `FROM 'name'` is inclusive**: the
page starts *with* the named task, where every type above starts after it.
Sandbox, 2026-10-24, 23 suspended tasks in one schema with names chosen to
stress ordering and quoting (`T_A`, `T_A1`, `T_A_1`, `"lower_task"`, `"WITH
SPACE"`, `O'QUOTE`, ...): `LIMIT 5` returned 5 rows, and paging by the last
name returned every task, in the unpaged order, with each page's first row
repeating the previous page's last. `FROM 'O''QUOTE'` started with `O'QUOTE`.
`SHOW TASKS IN DATABASE ... LIMIT 3 FROM 'T$DOLLAR'` behaved the same. The
order is byte order on the name (`Mixed` < `O'QUOTE` < `T$DOLLAR` < `T0` <
... < `lower_task` < `zz_last`).

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
was never observed firing in the wild.

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

`SHOW GRANTS ON PIPE` and `ON TASK` return the same nine columns (sandbox,
2026-10-24).

A grant is one row, never two (sandbox, 2026-10-24). Granting the same
privilege to the same role again leaves the row unchanged, `created_on`
included. Granting it again `WITH GRANT OPTION` replaces the row, with
`grant_option = 'true'` and a new `created_on`. Across all 86 grant rows on
`RENETEST1`–`3` and their schemas, the `show_grants` key in `schema.sql`
was unique.

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

On the sandbox (2026-10-24), three CSV files `PUT` to an internal stage and
sent with `ALTER PIPE ... REFRESH` showed up in
`INFORMATION_SCHEMA.COPY_HISTORY` 31 s later. Files loaded in one batch
share one `LAST_LOAD_TIME` and one `PIPE_RECEIVED_TIME`, so `FILE_NAME` is
what keeps `(FILE_NAME, LAST_LOAD_TIME)` unique. A file with a bad row read
`STATUS = 'Load failed'`, `ROW_COUNT = 0`, with `FIRST_ERROR_MESSAGE` and
`FIRST_ERROR_LINE_NUMBER = 2` set. The same file name `PUT` again with
`OVERWRITE = TRUE` and new content, then refreshed, was not loaded again and
added no row, so one pipe lists a file name at most once while Snowpipe's
load metadata remembers it.

`INFORMATION_SCHEMA.COPY_HISTORY`'s `TABLE_NAME` argument and
`TASK_HISTORY`'s `DATABASE_NAME`/`SCHEMA_NAME`/`TASK_NAME` arguments accept a
pyformat `%s` bind. The rows equalled those of the `quote_literal` spelling.

`TABLE_NAME` is read like an identifier path, sandbox, 2026-11-02. For a
table created as `"t lower"`, `'DB.SCH.t lower'` failed with `Table
'DB.SCH."T LOWER"' does not exist`, while `'DB.SCH."t lower"'` and
`'"DB"."SCH"."t lower"'` worked. For an upper-case table, the quoted
`'"DB"."SCH"."T_UP"'`, the unquoted `'DB.SCH.T_UP'` and the lower-case
`'db.sch.t_up'` all worked.

The function rejects a `PIPE_NAME` argument given without `TABLE_NAME`:
`missing required argument [TABLE_NAME]` (sandbox, 2026-11-02).

## Task history

An unqualified `INFORMATION_SCHEMA.TASK_HISTORY(...)` resolves against the
session's current database. With none set, it failed on a live account with
"invalid identifier", regardless of the `DATABASE_NAME` argument.

`QUERY_ID` and `QUERY_START_TIME` are `NULL` exactly when a run failed
before a query was submitted (sandbox, 2026-10-24). Without the
account-level `EXECUTE TASK` privilege on the owner role, every run fails
that way, with `091089: Cannot execute task, EXECUTE TASK privilege must be
granted to owner role`. That is what the sandbox's earlier test runs hit.
`SYSADMIN` does hold `USAGE` on warehouse `WH`; the warehouse was never the
cause. With `EXECUTE TASK` granted for the test and revoked afterwards:

- runs of `AS SELECT 1` read `SUCCEEDED`, with both columns set;
- runs of `AS SELECT 1/0` read `FAILED`, `100051: Division by zero`, also with
  both columns set.

Rows with a `NULL` `QUERY_ID` survived a `(_scope, QUERY_ID)` key. Two
such runs stored as two rows, over two refresh passes, because SQLite
treats `NULL`s in a key as distinct. A window pass reads its overlap again
on every refresh, so the `ACCOUNT_USAGE` task mirrors are keyed by
`(SCHEDULED_TIME, ATTEMPT_NUMBER)` per task instead.

Snowflake's documentation for `ACCOUNT_USAGE.TASK_HISTORY`, read
2026-11-03: "the view does not return SCHEDULED or EXECUTING task runs";
`STATE` is one of `SUCCEEDED`, `FAILED`, `CANCELLED`, `SKIPPED`; "Latency for
the view may be up to 45 minutes." A run's row therefore appears once, after
the run has finished, and does not change.

The view's rows on the sandbox, read 2026-11-03 (8245 runs over a year, 10
of them `FAILED`, 8 of those with a `NULL` `QUERY_ID`):

- `COMPLETED_TIME` was set on every row, the failed-before-submission runs
  included. That is what a window over task runs filters on: a run is
  published only once it finishes, so its `SCHEDULED_TIME` can lie before
  any short window. The longest run took 1318 s from scheduled to completed.
- `ATTEMPT_NUMBER` was set on every row, always 1. No two rows shared
  `(DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME)`.
- `RUN_ID` is not a key for one run: 8140 distinct values over 8245 rows, as
  expected of an identifier shared by the tasks of one graph run.

The same runs in both views, sandbox, 2026-11-03: 6 runs of one task, matched
by `QUERY_ID` between `ACCOUNT_USAGE.TASK_HISTORY` and
`INFORMATION_SCHEMA.TASK_HISTORY`. `SCHEDULED_TIME`, `QUERY_START_TIME`,
`COMPLETED_TIME` and `ATTEMPT_NUMBER` were equal in every pair. Only
`SCHEDULED_BY_USER` (`DROPPED_USER(0)` against `NULL`) and `ERROR_MESSAGE`
(`''` against `NULL`) differed. Unlike a load's `LAST_LOAD_TIME` (see [Copy
history](#copy-history)), a run's times are not stamped separately by each
view. `INFORMATION_SCHEMA` also listed the task's next run as `SCHEDULED`,
with a `NULL` `QUERY_ID` but `SCHEDULED_TIME` and `ATTEMPT_NUMBER` set. The
other tasks with recent runs on the sandbox are Snowflake's own, in schemas
of the `SNOWFLAKE` database whose `INFORMATION_SCHEMA` the role may not
read, so the sample is small.

## Pipe target table

No named field holds the table a pipe loads into. Snowflake's documentation,
read 2026-11-02:

- `SHOW PIPES`, `INFORMATION_SCHEMA.PIPES` and `ACCOUNT_USAGE.PIPES` name it
  only inside `definition`, the `COPY` statement. A Snowpipe Streaming default
  pipe takes its name from its table, but that kind is Snowflake's own.
- None of `SYSTEM$PIPE_STATUS`'s documented fields names it.
- `ACCOUNT_USAGE.OBJECT_DEPENDENCIES` does not list pipes among the objects
  it tracks.
- `COPY_HISTORY`, access history and lineage name it only for a load that
  happened.

How the `COPY` resolves an unqualified target, sandbox, 2026-11-02. Every
pipe was created in schema A, with the session's current schema set as shown:

| Session schema | `COPY INTO` | Result |
| --- | --- | --- |
| B | `RES_ONLY_B` (exists only in B) | created |
| B | `RES_ONLY_A` (exists only in A) | `Table 'RES_ONLY_A' does not exist` |
| A | `RES_ONLY_A` | created |
| A | `RES_ONLY_B` | `Table 'RES_ONLY_B' does not exist` |
| B | `NIVARIA_PROBE_A.RES_ONLY_A` | created |
| B | `RENETEST1.NIVARIA_PROBE_A.RES_ONLY_A` | created |
| A | `NIVARIA_PROBE_B.RES_ONLY_B` | created |

An unqualified target therefore resolves against the session's schema at
`CREATE PIPE` time, not against the pipe's own schema. `SHOW PIPES` keeps the
definition as written (`COPY INTO RES_ONLY_A FROM ...`), so the text alone
does not say which table that is. The `CREATE PIPE` documentation is silent on
this.

All 1872 pipe definitions on the three sandbox databases start with `COPY
INTO`; 1871 name a fully qualified, unquoted target.

## Pipe execution state

`SYSTEM$PIPE_STATUS` takes one pipe, as a constant. A column reference, as
in a `SELECT` over `INFORMATION_SCHEMA.PIPES`, fails with `argument 0 to
function SYSTEM$PIPE_STATUS needs to be constant`. It needs no warehouse.
Neither `SHOW PIPES` nor `INFORMATION_SCHEMA.PIPES` nor
`ACCOUNT_USAGE.PIPES` has a state column. Timings are in
[measurements.md](measurements.md#pipe-status).

How `invalid_reason` relates to `executionState`, on the sandbox
(2026-10-24):

| What was done to the pipe | `invalid_reason` | `executionState` |
| --- | --- | --- |
| nothing | `NULL` | `RUNNING` |
| `ALTER PIPE ... SET PIPE_EXECUTION_PAUSED = TRUE` | `NULL` | `PAUSED` |
| its stage dropped | `Pipe is invalid because stage STG_GONE dropped.` | `STOPPED_STAGE_DROPPED` |
| its named file format dropped | `NULL` | `RUNNING` |
| its target table dropped | `NULL` | `RUNNING` |

A paused pipe therefore reads exactly like a running one in `SHOW PIPES`.
Dropping a file format or a target table goes unnoticed by Snowflake itself
until a load is attempted.

Batching calls with `UNION ALL`, on the sandbox (2026-10-25):

- The function returns a JSON string. For a healthy pipe it carried exactly
  `executionState`, `pendingFileCount` and `pendingHistoryRefreshJobsCount`.
- The argument `'"DB"."SCHEMA"."p lower"'`, quoted identifiers inside a
  string literal, resolves a lower-case name with a space as written.
- One pipe that does not exist fails the **whole statement** at compile time,
  with `002003 (02000): SQL compilation error: Pipe '"DB"."SCHEMA"."NAME"'
  does not exist or not authorized.` (errno 2003). A missing schema fails the
  same way. The other pipes in the statement return nothing.

What it reports about loads, on the sandbox (2026-11-03), for a new pipe
from an internal stage, each file sent with `ALTER PIPE ... REFRESH`:

- Before the first file, the same three fields as above. All 1871 live pipes
  on the sandbox, none of which had ever loaded, returned only those.
- About 30 s after a good file was sent, `lastIngestedTimestamp` and
  `lastIngestedFilePath` appeared. The timestamp equals the load's
  `PIPE_RECEIVED_TIME` in `COPY_HISTORY` and comes before its
  `LAST_LOAD_TIME`: it says when the pipe received the file, not when it was
  loaded. It is written in UTC with a `Z`, such as
  `2026-11-03T20:11:26.046Z`.
- A file that failed to load (`STATUS = 'Load failed'`) moved both fields on
  to it, like a good one.

On a production account (2026-11-05), a running pipe that had loaded about an
hour earlier reported `lastReceivedMessageTimestamp` and
`lastPulledFromChannelTimestamp` as its only timestamps: no
`lastIngestedTimestamp` and no `lastForwardedMessageTimestamp`. Snowflake's
documentation says `lastPulledFromChannelTimestamp` applies to auto-ingest
loads only, so this is presumably an auto-ingest pipe. The documentation lists
the `lastIngested...` fields with no such restriction, says fields appear only
"if applicable", and calls `lastIngestedTimestamp` the last file "loaded
successfully", which the failed load above contradicts. Neither timestamp
reported says that this pipe took in a file: the received message may not
apply to the pipe, and a pull may find nothing. So nivaria shows none of these
timestamps. Whether the kind of pipe explains the difference was not tested,
nor whether the fields disappear some time after a pipe's last file.

## Result types

Every timestamp column nivaria reads from `SHOW` (databases, schemas, tables,
views, pipes, stages, tasks, grants, warehouses, roles, materialized views),
from `INFORMATION_SCHEMA.TABLES`, `COPY_HISTORY` and `TASK_HISTORY` is
`TIMESTAMP_LTZ` (sandbox, 2026-10-24). The driver returns those as aware
`datetime`s in the session's `TIMEZONE`, which on the sandbox is
`America/Los_Angeles`, not the machine's. Only a user table's
`TIMESTAMP_NTZ` column, in the Data tab, arrives naive.

What the driver (snowflake-connector-python 4.7.3) returns for a Data tab's
`SELECT *`:

| Type | Python value |
| --- | --- |
| `NUMBER(p,s>0)`, `DECFLOAT` | `Decimal`; `DECFLOAT` is rounded to 28 significant digits (`1.234567890123456789012345678901234567e100` arrived as `1.234567890123456789012345679E+100`) |
| `NUMBER(p,0)` | `int`, all 38 digits |
| `FLOAT` | `float`, including `nan`, `inf`, `-inf` |
| `DATE`, `TIME`, `TIMESTAMP_*` | `date`, `time`, `datetime`, cut to microseconds (`TIME(9)` `23:59:59.123456789` arrived as `23:59:59.123456`) |
| `TIMESTAMP_TZ` | aware, with the value's own offset |
| `BINARY` | `bytearray` |
| `VARIANT`, `OBJECT`, `ARRAY`, `MAP`, structured types | `str` of indented JSON; a VARIANT string keeps its quotes, an `ARRAY`'s SQL `NULL` reads `undefined`, a VARIANT JSON `null` reads `null` |
| `GEOGRAPHY`, `GEOMETRY` | `str` of indented GeoJSON (`GEOGRAPHY_OUTPUT_FORMAT` is `GeoJSON`) |
| `VECTOR` | `list` of `int`/`float` |

Their `cursor.description` type codes are VARIANT 5, OBJECT 9 (`MAP` and a
structured `OBJECT` too), ARRAY 10, GEOGRAPHY 14, GEOMETRY 15, VECTOR 16,
and `DECFLOAT` reports 0 (`FIXED`).

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

## Snowsight links

Snowflake does not document Snowsight's object routes. On the sandbox on
2026-11-02, Snowsight's address bar showed these for objects opened from
**Catalog » Database Explorer** (org, account, database and schema replaced by
placeholders):

```text
https://app.snowflake.com/myorg/myacct/#/data/databases/DB/schemas/SC/table/probe%20lower
https://app.snowflake.com/myorg/myacct/#/data/databases/DB/schemas/SC/stage/PROBE_STAGE
https://app.snowflake.com/myorg/myacct/#/data/databases/DB/schemas/SC/pipe/PROBE_PIPE
https://app.snowflake.com/myorg/myacct/#/data/databases/DB/schemas/SC/task/PROBE_TASK/run-history
```

- The base is `https://app.snowflake.com/<orgname>/<account_name>/`, lowercase.
  Snowflake's Streamlit sharing documentation uses the same base for its app
  links.
- Names appear as stored, case kept, percent-encoded: the quoted table
  `"probe lower"` is `probe%20lower`.
- `/run-history` on the task is the tab Snowsight opened by default.
- The `view` segment and the bare database and schema routes
  (`#/data/databases/DB`, `…/schemas/SC`) were not checked live; they are the
  ones DataHub's Snowflake connector builds (`SnowsightUrlBuilder`).
- Snowflake documents two private Snowsight hosts, a regionless
  `app-<org>-<account>.privatelink.snowflakecomputing.com` and a regional
  `app.<cloud_region_id>.privatelink.snowflakecomputing.com`. The production
  account (PrivateLink) is reached at the regional one, with the public form's
  `/<org>/<account>/` path:
  `https://app.<cloud_region_id>.privatelink.snowflakecomputing.com/<org>/<account>/`.
  Which host resolves depends on the DNS the account's network admins set up,
  and `SYSTEM$GET_PRIVATELINK_CONFIG()`, which reports both, needs
  `ACCOUNTADMIN`; hence the per-account Snowsight URL setting rather than a
  derived private host.
- Not checked: whether a link opened while signed out reaches the object
  after sign-in.

## externalbrowser login

The installed driver's `AuthByWebBrowser` waits on a raw `select.select()` with
no timeout argument anywhere in that code path, confirmed by reading it: no
connect kwarg reaches it, and `socket.setdefaulttimeout()` would not either,
since `select.select()` ignores a socket's `.settimeout()`. An abandoned
browser tab therefore blocks the connection attempt indefinitely.
