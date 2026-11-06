# Measurements

This document collects the timings and data volumes that nivaria's design
decisions rest on, with what each was measured against.

The decisions themselves are in [DESIGN.md](DESIGN.md); account details are in
[snowflake-behavior.md](snowflake-behavior.md#accounts-probed).

## Round trips

Against the reference account: **~187 ms per `SHOW` round trip regardless of
size**, while 383 rows came back in 210 ms.

Consequence for paging a 2145-table database: pages of 5 would take ~80 s,
pages of 1000 under a second.

## Database priming

One `SHOW TABLES IN DATABASE` for 2145 tables across 54 schemas took ~1 s,
against ~10 s for 54 separate `IN SCHEMA` calls.

## History volume

On one production schema, a raw, unreduced `ACCOUNT_USAGE.COPY_HISTORY` count
over a 13-day window came back at **433 892 rows** across 190 pipes. The same
window through the `PIPE_ACTIVITY` reduction (`QUALIFY ROW_NUMBER() OVER
(PARTITION BY PIPE_NAME ORDER BY LAST_LOAD_TIME DESC) = 1`) returned **190 rows
for 190 distinct pipes**, a ~2284x collapse. Without the reduction, an account
with ~400 pipes hung the app for minutes.

## History latency

One pipe's or task's history statement, on the sandbox, 2026-11-02. Five runs
each with `USE_CACHED_RESULT = FALSE`, median (min–max); a `SELECT 1` round
trip took 0.06 s.

| Statement | Rows | Time |
| --- | --- | --- |
| `pipe_activity_history`, never-loaded pipe, 90 days | 0 | 6.45 s (3.09–14.30) |
| the same, 14 days | 0 | 1.21 s (1.03–5.37) |
| the same, 2 days | 0 | 1.16 s (0.96–4.58) |
| `pipe_activity_history`, pipe with 6 loads, 90 days | 6 | 6.66 s (3.65–9.11) |
| the same, 14 days | 6 | 1.32 s (1.23–1.86) |
| `pipe_activity`, one schema, 90 days | 0 | 2.62 s (2.49–2.68) |
| `pipe_history` (`INFORMATION_SCHEMA`, 14 days) | 0 | 0.16 s (0.13–0.19) |
| `task_activity_history`, 2163 runs, 90 days | 2163 | 9.74 s (4.61–24.82) |
| `task_activity_history`, no runs, 90 days | 0 | 3.77 s (1.13–5.48) |
| `task_history` (`INFORMATION_SCHEMA`, 7 days) | 0 | 0.18 s (0.17–0.34) |

The `ACCOUNT_USAGE` window, not the row count, sets the cost: 90 days costs
about five times 14 days with nothing to return. The `INFORMATION_SCHEMA`
functions answer in well under a second.

## ACCOUNT_USAGE windows

`ACCOUNT_USAGE.COPY_HISTORY` on a production account with 21.8 million loads
in 90 days, X-Small warehouse, median of three runs:

| Statement | Busy schema (381 248 loads in 90 days) | Quiet schema (1 load) |
| --- | --- | --- |
| `pipe_activity`, 90 days | 31.2 s (30.2 s executing) | 7.6 s (4.9 s executing) |
| `pipe_activity`, 2 days | 2.0 s (1.0 s executing) | 2.5 s (0.9 s executing) |
| `pipe_activity_history`, one pipe, 90 days | 5.2 s, 5000-row cap reached | 3.4 s |

Compiling took 0.7–2.7 s of each. The cost follows the time window across
the whole account, not the schema's own loads: the quiet schema's 90 days
cost almost four times its 2 days. This is why a log scope is fetched in full
only once and then refreshed by window passes (see
[DESIGN.md](DESIGN.md#window-passes)).

## Column scale

`SHOW COLUMNS IN DATABASE` returned 11 511 columns for 614 tables across 25
schemas on the sandbox; one production schema alone has 6655 columns.

## Pipe status

`SYSTEM$PIPE_STATUS` takes one constant pipe name per call (see
[snowflake-behavior.md](snowflake-behavior.md#pipe-execution-state)). On the
sandbox, 2026-10-24:

| Shape | Pipes | Time |
| --- | --- | --- |
| one statement per pipe | 27 | 2.5 s (~94 ms each) |
| one `UNION ALL` statement, one call per pipe | 22 | 0.9 s |
| one `UNION ALL` statement, one call per pipe | 587 | 14.5 s |
| one `UNION ALL` statement, one call per pipe (2026-10-25, three runs) | 50 | 0.8–1.3 s |

Batching saves the round trips, but the server still spends ~25 ms per
pipe. The 50-pipe batch is the shape the Pipes (n) list's status pass
sends (`PIPE_STATUS_BATCH`).

## Search

Corpus loads:

| Corpus | Size | Load time |
| --- | --- | --- |
| objects or schemas | ~2000 objects | ~5 ms |

Ranking a whole object corpus, worst-case query:

| Objects | Time |
| --- | --- |
| 2k | 3 ms |
| 10k | 14 ms |
| 50k | 70 ms |
| 100k | 150 ms |
