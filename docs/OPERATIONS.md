# nivaria operations

This document describes how nivaria behaves while it runs: what it sends to
Snowflake and when, its limits and timeouts, how it fails and recovers, the
files it keeps, and how to troubleshoot it.

The reasons behind this behaviour are in [DESIGN.md](DESIGN.md); the timings
in [measurements.md](measurements.md); what Snowflake itself was observed to do
in [snowflake-behavior.md](snowflake-behavior.md).

## What nivaria sends

Every statement is built in `gateway/queries.py`, and every one is read-only:
`SHOW` commands, `SELECT`s from `INFORMATION_SCHEMA` and
`SNOWFLAKE.ACCOUNT_USAGE`, the Data tab's `SELECT * ... LIMIT n`, and `USE
ROLE` / `USE WAREHOUSE` when you switch. The Log panel (`F1`) shows each
statement as it is sent.

Nothing waits on Snowflake: the screen always shows what is cached and
updates when a fetch lands.

## Warehouse and privileges

The tree, every object list and the column lists come from `SHOW`, which
needs no warehouse and costs no credits. A session with no warehouse can
browse everything.

These need a running warehouse and stay blank without one:

- Last DDL / Last Altered in the Tables and Views lists;
- pipe activity (Pipes list) and task activity (Tasks list);
- pipe and task History;
- Tags on schemas, databases and columns;
- the Data tab.

Pipe and task activity, and the older half of each History, read
`SNOWFLAKE.ACCOUNT_USAGE` and also need `IMPORTED PRIVILEGES` on the
`SNOWFLAKE` database. The scope-by-scope list is in
[DESIGN.md](DESIGN.md#the-second-source-scopes).

The Grants tabs show only what the active role may see (what it owns, holds
`MANAGE GRANTS` on, or reaches through `ACCOUNTADMIN`/`SECURITYADMIN`), and
only direct grants. A short Grants tab under a lesser role is correct.

Grants and Data cost one query per object, so they are fetched only while
their tab is shown. The Data tab wakes the warehouse; for a view, it runs the
view's query.

## Background load

nivaria refreshes only what is on screen. When the footer Countdown reaches
zero (every **cache age** seconds, 900 by default), the parts of the screen
older than that are fetched again at background priority. Navigating
somewhere new restarts the countdown. Nothing else is fetched in the
background except the initial requests after connecting. A cache age of `0`
stops background refreshing; `F5` still refreshes the node under the cursor.

The `ACCOUNT_USAGE` half of a pipe's history is refetched at most every 13
days, since it only changes as data ages out of the 14-day
`INFORMATION_SCHEMA` window; the Pipes list's activity columns use the same
interval.

**Pre-warm cache** and the background refresh share the connection pool with
browsing at the lowest priority, so they never delay what you are doing.

Fetches are deduplicated and the newest request wins: a burst of expands or a
fast scroll does not queue the same work twice. Switching database drops
queued work for the old one.

## Connections and MFA

- The connect dialog opens **one** connection. More, up to the **connection
  pool size** setting, only after you confirm (OAuth: without asking), and
  always one at a time, so prompts never arrive in parallel.
- Each connection attempt gives up after **20 seconds** of waiting for
  browser or MFA approval. Browser SSO (`externalbrowser`) is the exception:
  an abandoned browser tab can leave the dialog waiting indefinitely; restart
  nivaria.
- Sessions are kept alive, and the driver's token cache is on, so later
  connections and sessions usually do not prompt again.
- The Pool gauge shows `(in use / total)`. A job that needs a connection while
  all are busy waits.
- A connection found dead is replaced on the spot when you are the one
  waiting. Background work never opens a connection, so it cannot raise an MFA
  prompt while you are away: it drops the dead connection instead, and the
  gauge's total shrinks until your next action reopens it.

### Switching role or warehouse

**Role ...** and **Warehouse ...** run `USE ROLE` / `USE WAREHOUSE` on every
pooled connection, waiting for running statements first. If it fails on one
connection after succeeding on another, the error is shown and the pool is
left split; switch again or reconnect.

A role switch stops background fetching, switches to that role's own cache
file and starts over. Each (account, role) has its own cache, so a role used
before is still filled. A warehouse switch leaves the cache alone.

## Limits and timeouts

| Limit | Setting | Default | When it trips |
| --- | --- | --- | --- |
| Rows read from one statement | result row ceiling | 200 000 | The fetch fails with an error; the previous rows stay on screen. |
| Rows in one history statement | history row cap | 5000 | The newest rows are kept; a note above the History table says so. |
| Rows in a data preview | data preview rows | 100 | The Data tab shows that many rows, in no particular order, and says so. |
| Data preview runtime | data preview timeout | 15 s | Snowflake cancels the query; the Data tab shows the error and retries on the next background refresh while open. |
| Pages per refresh | max pages per refresh | 50 | The refresh fails with an error rather than running on. |
| Whole-database result size | prime chunk threshold | 10 000 | The fetch is redone per schema, then per table. |

The defaults are starting points; raise or lower them in Settings if your
account needs it. See [DESIGN.md](DESIGN.md#bounding-a-result) for why each
exists.

## Failure and recovery

- **A failed refresh keeps the old rows** and reports the error. Nothing is
  removed until a refresh succeeds.
- **Something dropped in Snowflake** disappears the next time nivaria trips
  over it: the failed fetch triggers a refresh of the list above it, which
  removes the node. "Does not exist" is shown as a warning.
- **A failed fetch is retried** by the next background refresh, or when you
  navigate back to it — not in a loop.
- **Reconnect** with `Ctrl+O` (Connection › Open...). It replaces the whole
  pool and reopens the cache for the new identity.
- The cache is never required: if it cannot be opened, nivaria runs with
  nothing cached.

## Quitting

`Ctrl+Q` with statements still running stops new work and shows "Waiting for
background jobs to finish..." until they end, so every connection closes
cleanly. A second `Ctrl+Q` quits at once; the remaining statements are
abandoned and their results discarded.

## Files on disk

nivaria uses the platform's per-user directories (via `platformdirs`, app name
`nivaria`).

Config directory (`user_config_dir`):

| File | Content |
| --- | --- |
| `settings.json` | Settings that differ from their default. |
| `recent.json` | Recent connections: account, user, authenticator, role, warehouse, display name and selected databases. No passwords. |
| `credential_cache.json` | Windows only: the driver's login tokens, encrypted with DPAPI for the current Windows user. |

A `recent.json` that cannot be read shows no recents and is left untouched.

Cache directory (`user_cache_dir`):

| File | Content |
| --- | --- |
| `cache-<account>-<role>.db` (plus `-wal`, `-shm`) | The sqlite cache for one account and role: Snowflake metadata only. |

The cache is safe to delete at any time while nivaria is not running; it
refills on the next start. A cache written by a different nivaria schema
version is deleted and rebuilt automatically.

Data tab rows are never written to disk: they are held in memory and gone when
nivaria exits, reconnects or switches role.

On macOS the driver keeps login tokens in the Keychain, on Linux in a file
under `~/.cache/snowflake/`.

## Troubleshooting

- **See what nivaria is doing**: `F1` opens the Log panel with every
  statement as it is sent, before Snowflake answers, so a hung statement is
  visible too.
- **Blank columns or tabs**: the session has no warehouse, or the role lacks
  `IMPORTED PRIVILEGES` on `SNOWFLAKE` (see "Warehouse and privileges").
  Everything from `SHOW` still shows.
- **Every connection prompts again**: the driver's token cache is not
  persisting. nivaria depends on `snowflake-connector-python[secure-local-storage]`
  for that; on Windows it stores tokens in `credential_cache.json` because
  Windows Credential Manager cannot hold an OAuth token.
- **Connecting hangs with browser SSO**: an abandoned or closed browser tab
  is never noticed. Quit and start again.
- **Old data**: press `F5` on the node, or lower **cache age**.
- **A refresh fails with a row-ceiling or page-limit error**: the result was
  larger than the limit allows; raise the setting if the size is expected.
