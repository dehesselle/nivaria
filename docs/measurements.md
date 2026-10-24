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

Batching saves the round trips, but the server still spends ~25 ms per
pipe.

## Search

Corpus loads:

| Corpus | Size | Load time |
| --- | --- | --- |
| objects or schemas | ~2000 objects | ~5 ms |
| columns | 30k cached columns | ~55–75 ms |
| columns | 270k cached columns | ~700 ms |

Ranking a whole object corpus, worst-case query:

| Objects | Time |
| --- | --- |
| 2k | 3 ms |
| 10k | 14 ms |
| 50k | 70 ms |
| 100k | 150 ms |

At 270k cached columns, ranking a single query took 73–450 ms depending on how
selective it was. The column corpus routinely outnumbers the object corpus
10–20x, which is why ranking runs in a thread.
