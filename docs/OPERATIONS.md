# nivaria operations

This document describes how nivaria behaves while it runs: what it sends to
Snowflake and when, its limits and timeouts, how it fails and recovers, the
files it keeps, and how to troubleshoot it.

The reasons behind this behaviour are in [DESIGN.md](DESIGN.md); the timings
in [measurements.md](measurements.md); what Snowflake itself was observed to do
in [snowflake-behavior.md](snowflake-behavior.md).

## What nivaria sends

Every statement is built in `snowflake/queries.py`, and every one is read-only:
`SHOW` commands, `SELECT`s from `INFORMATION_SCHEMA` and
`SNOWFLAKE.ACCOUNT_USAGE`, the Data tab's `SELECT * ... LIMIT n`, and `USE
ROLE` / `USE WAREHOUSE` when you switch. Once per connect, `SELECT
CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT_NAME()` reads the names that
Snowsight links and the account label need; it uses no warehouse. At level
Debug, the Log panel (`F1`) shows
each statement as it is sent, and [data-retrieval.md](data-retrieval.md) lists
what each screen sends.

Every session reports itself as `nivaria` in
`SNOWFLAKE.ACCOUNT_USAGE.SESSIONS.CLIENT_ENVIRONMENT:APPLICATION`; join on
`SESSION_ID` to attribute `QUERY_HISTORY` rows. nivaria sets no `QUERY_TAG`, so
a tag set on your user or account still applies to its queries.

Nothing waits on Snowflake: the screen always shows what is cached and
updates when a fetch lands.

## Warehouse and privileges

The tree, every object list and the column lists come from `SHOW`, which
needs no warehouse and costs no credits. A session with no warehouse can
browse everything. The Pipes list's Execution State is read with
`SYSTEM$PIPE_STATUS`, which needs no warehouse either.

These need a running warehouse and stay blank without one:

- Last DDL / Last Altered in the Tables and Views lists, and Last Altered in
  the Stages list;
- pipe activity (Pipes list) and task activity (Tasks list);
- pipe and task History;
- Tags on schemas, databases and columns;
- the Data tab.

A statement refused for lack of a warehouse fails once and keeps its
connection, so it costs no new login and no MFA prompt.

Pipe and task activity, and the older half of each History, read
`SNOWFLAKE.ACCOUNT_USAGE` and also need `IMPORTED PRIVILEGES` on the
`SNOWFLAKE` database. The statement-by-statement list is in
[data-retrieval.md](data-retrieval.md#screens).

The Grants tabs show only what the active role may see (what it owns, holds
`MANAGE GRANTS` on, or reaches through `ACCOUNTADMIN`/`SECURITYADMIN`), and
only direct grants. A short Grants tab under a lesser role is correct.

Grants, Data and a schema's or database's Tags cost one query per object, so
they are fetched only while their tab is shown. The Data tab uses the warehouse;
for a view, it runs the view's query. Resting the cursor on a table or view also
uses the warehouse, for its column tags.

## Background load

nivaria refreshes only what is on screen. The footer Countdown shows how long
until the first part of the screen is older than **cache age** (300 seconds
by default); at zero, every part of the screen that old is fetched again at
background priority. Each part ages from its own last fetch, so refreshes
come as each part runs out, not all at once. Nothing else is fetched in the
background except the initial requests after connecting, and the object lists
of a selected database that have never been fetched, so that search can find
them: one `SHOW` per object type for that database, so five, sent once. A cache
age of `0` stops background refreshing; `F5` still refreshes what the screen
shows.
A cache age shorter than one fetch makes that part due again as soon as its
refresh lands, so it is refetched back to back, one fetch at a time.

Moving the tree cursor through a list sends nothing for the rows it passes. What
is cached shows at once, and what a node needs is only checked and fetched after
the cursor has rested on it for the **cursor settle delay**. Moving on also drops
what was queued for the node you left. A statement already running finishes, so
up to the pool's size can still be running when you land.

Pipe and task activity read `ACCOUNT_USAGE`, whose cost follows the time
window asked for rather than the rows returned: 8-31 s for 90 days of copy
history on a production account, about 2 s for 2 days (see
[measurements.md](measurements.md#account_usage-windows)). So each of these
is fetched in full (the fetch window) only the first time, and after a fetch
window is raised. Every later refresh asks only for the time since the
previous one, reaching back by the **lag margin** setting, and keeps
everything older. For a pipe that the Pipes list shows no load for, the
`ACCOUNT_USAGE` half of its history is not fetched at all: the Pipes list is
the answer, and the `INFORMATION_SCHEMA` half covers the days since.

The background refresh shares the connection pool with browsing at the
lowest priority, so it never delays what you are doing.

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
  waiting, one login at a time, so several dead connections do not open several
  browser windows at once. Background work never opens a connection, so it cannot raise an MFA
  prompt while you are away: it drops the dead connection instead, and the
  gauge's total shrinks until your next action reopens it.

### Switching role or warehouse

**Role ...** and **Warehouse ...** run `USE ROLE` / `USE WAREHOUSE` on every
pooled connection, waiting for running statements first. If it fails on one
connection after succeeding on another, the error is shown and the pool is
left split; switch again or reconnect.

A role switch stops background fetching, switches to that role's own cache
file and starts over. Each (account, role) has its own cache, so a role used
before is still filled. A warehouse switch leaves the cache alone and
refetches whatever on screen had failed.

## Limits and timeouts

| Limit | Setting | Default | When it trips |
| --- | --- | --- | --- |
| Rows read from one statement | result row ceiling | 200 000 | The fetch fails with an error; the previous rows stay on screen. |
| Rows in one history statement | history row cap | 5000 | The newest rows are kept; a note above the History table says so. |
| Rows in a data preview | data preview rows | 100 | The Data tab shows that many rows, in no particular order, and says so. |
| Data preview runtime | data preview timeout | 15 s | Snowflake cancels the query; the Data tab shows the error and retries on the next background refresh while open. |
| Pages per refresh | max pages per refresh | 50 | The refresh fails with an error rather than running on. |
| Whole-database result size | prime chunk threshold | 10 000 | The fetch is redone per schema. |

The defaults are starting points; raise or lower them in Settings if your
account needs it. See [DESIGN.md](DESIGN.md#bounding-a-result) for why each
exists.

## Failure and recovery

- **A failed refresh keeps the old rows** and reports the error. Nothing is
  removed until a refresh succeeds. Until then, a note above a list, a
  History or a Data table says when the rows shown were fetched. The same goes
  for a refresh that could not be written to the cache, on a full disk say.
- **Something dropped in Snowflake** disappears the next time nivaria trips
  over it: the failed fetch triggers a refresh of the list above it, which
  removes the node. "Does not exist" is shown as a warning.
- **A failed fetch is retried** one cache age after it failed, when you
  navigate back to it, or right after you pick a warehouse — not in a loop.
- **A login that fails** while replacing a dead connection, because a prompt
  went unanswered or the network is down, fails that fetch and the gauge's total
  shrinks. Your next action that needs a connection tries the login again, so
  nothing needs restarting. A browser login that does not finish in time shows
  one "Login timed out" warning however many fetches it failed; `F5` asks again.
  Any other failed login shows the driver's message. The Log panel has the
  driver's full text either way.
- **Reconnect** with `, a c` (Account › Connect). It replaces the whole pool
  and reopens the cache for the new identity.
- The cache is never required: if it cannot be opened, nivaria warns once and
  keeps everything in memory, so it starts from nothing next time.

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

An export (`, e`, see [MANUAL.md](MANUAL.md#exporting-a-table)) is written to the
user's Downloads folder, and from then on is yours: nivaria never reads or removes
it. If that folder does not exist or cannot be written to, the export fails with an
error message and nothing else happens.

On macOS the driver keeps login tokens in the Keychain, on Linux in a file
under `~/.cache/snowflake/`.

## Troubleshooting

- **See what nivaria is doing**: `F1` opens the Log panel. Set its level to
  Debug. Every statement from then on appears as it is sent, before Snowflake
  answers, so a hung statement is visible too. One sent before you lowered the
  level isn't kept.
- **Blank columns or tabs**: the session has no warehouse, or the role lacks
  `IMPORTED PRIVILEGES` on `SNOWFLAKE` (see "Warehouse and privileges").
  Everything from `SHOW` still shows.
- **Every connection prompts again**: the driver's token cache is not
  persisting. nivaria depends on `snowflake-connector-python[secure-local-storage]`
  for that; on Windows it stores tokens in `credential_cache.json` because
  Windows Credential Manager cannot hold an OAuth token.
- **Connecting hangs with browser SSO**: an abandoned or closed browser tab
  is never noticed. Quit and start again.
- **"The browser login did not finish in time" although you completed it**: the
  browser's answer never reached nivaria. The driver suggests checking the
  redirect URI. The Log panel only exists after connecting, so a failure on the
  first connection shows nothing there.
- **Old data**: press `F5`, or lower **cache age**. `F5` refreshes what the
  screen shows at once, whatever its age. For pipe and task activity it asks only
  for the time since the previous refresh, and `ACCOUNT_USAGE`'s own lag still
  applies.
- **A refresh fails with a row-ceiling or page-limit error**: the result was
  larger than the limit allows; raise the setting if the size is expected.
