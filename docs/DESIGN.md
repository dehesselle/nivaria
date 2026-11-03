# nivaria — design

This document describes how nivaria works and why: the context a change needs
before it touches anything.

It deliberately avoids file layout and code: read the modules for those. How to
use nivaria is in [MANUAL.md](MANUAL.md), how it behaves while running in
[OPERATIONS.md](OPERATIONS.md). What live Snowflake accounts were observed to do
is recorded in [snowflake-behavior.md](snowflake-behavior.md), and the timings
the decisions below rest on in [measurements.md](measurements.md). The names
used for parts of the screen (Object tree, Detail pane, Status footer, ...) are
defined in the manual's [Screen anatomy](MANUAL.md#screen-anatomy).

---

## Architecture

### The one rule

**The UI reads the local sqlite cache and nothing else.**

No keystroke, expand, selection or search ever waits on Snowflake. Everything
on screen came from sqlite. When the cache is missing or stale, the UI *asks*
for a refresh and carries on painting what it already has; the answer arrives
later as a message and the affected widget repaints.

Two consequences follow, and most of the rest of this document is downstream
of them:

- Because the UI shows cached data, **the age of that data is first-class** and
  permanently visible.
- Because fetching is asynchronous and can be slow, **it must be cancellable,
  deduplicated and prioritised**, and it must never leave the UI in a
  half-updated state.

### Layers

- **Cache** (`cache/`): one sqlite file per (account, role), mirroring
  Snowflake's `SHOW` results, plus the freshness ledger that makes refreshes
  surgical. See "The cache".
- **Gateway** (`gateway/`): the only code that talks to Snowflake. It builds
  every statement (`queries.py`), owns the connection pool and logs each
  statement before it is sent. It is a protocol, so tests substitute a fake.
  See "Fetching" and "The connection pool".
- **Fetch service** (`service/`): background workers, one per pool
  connection, that run one page per job, write into the cache and post a
  message naming the scope they changed. See "The fetch scheduler".
- **UI** (`ui/`): the Textual screens, the Object tree, the detail views and
  search. It reads the cache, requests refreshes and repaints on messages.

---

## Core principles

These are the invariants a change must not break. Each links to the section
that explains it in full.

1. **The UI reads the sqlite cache and nothing else**, so no interaction ever
   waits on Snowflake. See "The one rule".
2. **Tree and detail data come from `SHOW`**, because `SHOW` needs no
   warehouse. A fact only a `SELECT` can supply is its own scope that degrades
   blank on its own, so a session without a warehouse still shows everything
   else. See "`SHOW`, because it needs no warehouse" and "A second source".
3. **The cache mirrors Snowflake.** One table per `SHOW`, columns named as
   Snowflake names them; nivaria's own bookkeeping is `_`-prefixed so it cannot
   collide; column headers use Snowflake's words. Inventing vocabulary loses
   information and confuses anyone who knows Snowflake. See "Shape" and
   "Column headers mirror Snowflake".
4. **Scopes are independent.** Each query is self-contained, so there is no
   ordering or dependency between scopes, and no deadlock surface. See
   "Scopes".
5. **A refresh is a pass, and a finished pass deletes exactly the rows it is
   responsible for**: every row it did not rewrite for an ordinary pass, what
   its window supersedes for a log scope's window pass. Rows are deleted only
   when a pass finishes, a write carrying a stale generation is rejected, and
   a failed pass never deletes, so the UI never flashes empty and yesterday's
   data beats none. See "The freshness ledger" and "Window passes".
6. **One cache file per (account, role)**, because `SHOW` output is filtered
   by role and the SQL carries no role. `FetchService` is stopped *before*
   `ConnectionPool.set_role` changes the role, or a worker writes the new
   role's rows into the old role's file. See "One cache file per identity".
7. **Every edit to `schema.sql` or `search.sql` bumps `SCHEMA_VERSION`**,
   because `IF NOT EXISTS` would never apply it to an existing file. Nothing is
   migrated; a test pins the scripts' digest to the version. See "A schema
   version".
8. **Table data never reaches the disk.** Data-preview rows can be personal
   data and live in an in-memory database only. See "Table data never reaches
   the disk".
9. **Round trips dominate; payload is nearly free.** No statement whose count
   scales with the number of objects or rows when a batch form exists, and a
   reduction (one row per pipe) happens in SQL, not in Python. See "Round
   trips dominate".
10. **Every result is bounded**, by three independent layers plus row caps on
    the history statements. See "Bounding a result".
11. **No keyset paging on the database-wide prime**: object names repeat
    across schemas, so resuming after a name skips rows. See "Database
    priming".
12. **Background work never opens a connection, and connections open one at a
    time**, because each new connection can mean an MFA prompt. See "The
    connection pool".
13. **An update never moves the tree cursor, and a highlight only shows.**
    Revealing is for jumps from outside the tree. See "The cursor must not
    move" and "A highlight is not a jump".
14. **Staleness is decided in one place**, `models.due_in`, and every scope
    ages on the same cache age. The sweep and the countdown reach it through
    `Entry.due`, the tree and the detail views through `models.is_stale`, so
    none of them can drift apart. A scope that is expensive to fetch in full
    is made cheap to refresh, never refreshed less often. See "Staleness is
    decided in one place" and "The staleness sweep".
15. **nivaria stores no password.** The driver owns every credential. See
    "Credentials and profiles".
16. **Modules outside the UI layer do not import Textual, and the gateway is a
    protocol.** That keeps the cache, scheduler, matcher and formatting
    testable as plain functions, and lets every test run offline against a
    fake gateway without importing the Snowflake driver.
17. **A `Menu`/`MenuItem` id must not match an `app.tcss` id**, because the id
    is also a CSS id and a clash garbles the menu bar. See "Shared widgets".

---

## The cache

### Shape: mirror Snowflake, don't reinterpret it

The bottom layer is **one table per `SHOW` command, with the result columns
named exactly as Snowflake names them** — `created_on`, `database_name`,
`cluster_by`, `invalid_reason` and so on. Column sets were verified against a
live account rather than taken from documentation (see
[snowflake-behavior.md](snowflake-behavior.md#verified-column-sets)).

Only a useful subset of each result is mirrored; columns Snowflake returns that
we do not mirror are dropped silently on ingest, so Snowflake adding a column
is a non-event.

Kinds are **not** collapsed into one generic table. `SHOW PIPES` has
`definition` and `notification_channel`; `SHOW TABLES` has `rows`, `bytes`,
`cluster_by`. A lowest-common-denominator table would lose those and invent
vocabulary that does not exist in Snowflake. The repetition is the cheaper
price.

Two derived layers sit on top, and only these two:

- a **uniform object view** used by search, flattening every type to
  `(database, schema, name, kind)` — `kind` there is Snowflake's own column
  name, kept because this layer still mirrors; nivaria's own word for the
  same idea is `ObjectType` (see "Vocabulary" below);
- the **freshness ledger**, below.

Bookkeeping columns are underscore-prefixed so they can never collide with a
Snowflake column name.

### Vocabulary: Snowflake's words, and where nivaria's differ

Snowflake's own terms are used throughout, in identifiers as well as prose,
and column headers follow the same rule (see "Column headers mirror
Snowflake"). Where nivaria needs a name Snowflake has none for, it is
obviously nivaria's: `Scope`, `CacheState`, `NodeType`, the `_`-prefixed
bookkeeping columns.

The one place the two collide is **type**. Snowflake's docs, `GRANT ... ON
<object_type>`, `INFORMATION_SCHEMA.TABLES.TABLE_TYPE` and Snowsight's own
filters all call "table vs view vs pipe" an *object type*, so that is what
`ObjectType` is called here. But several `SHOW` results also carry a column
literally named `kind` — `SHOW DATABASES` returns `STANDARD`/`IMPORTED
DATABASE` in it, `SHOW TABLES` a per-row detail — and those are mirrored
under that name, unchanged, because the mirror layer never renames anything.
So `kind` in `schema.sql`, in `search.sql` and in a `TableSpec.columns` tuple
is always Snowflake's column; `type` everywhere else is nivaria's word for
the concept. `Repository.search_corpus` is the one function that crosses the
seam, reading `row["kind"]` into an `ObjectType`.

### Scopes: the unit of everything

A **scope** is one separately-refreshable set of rows — the databases list, one
database's schemas, one schema's tables, one table's columns. A scope is a
single hashable value, which is what lets deduplication, update routing and
staleness all be dictionary lookups.

Scopes are **independent of one another**. It is tempting to think a schema's
tables must wait for that database's schema list to finish refreshing, but each
query is self-contained, so they simply run concurrently. There is no
dependency graph, no ordering constraint and no deadlock surface.

If a schema really was dropped, its tables fetch fails with "does not exist",
that failure is recorded, and the failing job asks for the listing one level up
(`models.containing_scopes`), whose refresh removes the node. That request is
made after the fact and never waited on, so it adds no ordering. Waiting for the
schema list's own next pass instead would leave the node standing until the TTL
sweep, which every navigation postpones: a drop made elsewhere is invisible to
nivaria until a statement trips over it, so tripping over it is the trigger.

"Does not exist" is recognised by the driver's error number (2003), not its
message text, which varies by object kind; it is reported as a warning rather
than an error, and identical notices are folded, since one visit to a dropped
schema fails its Grants and Tags together. It climbs one level per failure: a
table's columns name only the table, so a dropped *schema* found that way first
refreshes the schema's object listings, and those are requested per schema on
purpose -- a database-wide prime would succeed and leave the schema node
standing, while `SHOW ... IN SCHEMA` fails the same way and carries the refresh
up.

### One cache file per identity, not a role dimension on Scope

A scope is keyed by database/schema/object/table — never by role. That is
deliberate, not an oversight: `database` can be part of a scope key because it
is explicit in the statement text (`SHOW SCHEMAS IN DATABASE X`), but role has
no such syntax. It is pure session state, invisible in the SQL, so "which
role was active" cannot be recovered from a scope alone — it would have to be
stamped onto every row at fetch time from whatever the connection's role
happened to be *right then*, which is a much bigger seam to open (every scope
constructor, every mirror table's key, a schema change) for a problem that
has a far cheaper fix.

That fix: `SHOW` output is filtered by role, so the whole cache — not one
table, not one scope type, all of it — is only ever valid for the role that
was active while it was built. So instead of tagging rows, nivaria keeps a
**separate sqlite file per (account, role)** (`paths.cache_file`), and treats
switching roles as switching *which file* `Repository` points at, the same way
`open_connect` swaps in a whole new `ConnectionPool`. Within one file,
everything above (scopes, the pass protocol, staleness) holds unchanged,
because it is correct for a single, fixed identity.

This is also why a switch does not cost a wipe: switching back to a role
visited earlier in the session reopens that role's own file, warm, rather than
refetching everything. An admin who alternates between two roles all day
pays one refetch per role the *first* time they use it, not every time they
switch. Account is part of the key too, not just role, so two different
accounts that happen to use a same-named role (a generic `SYSADMIN` exists in
plenty of accounts) cannot share a file either. Warehouse is deliberately
excluded from the key: `SHOW` needs no warehouse (see below), so nothing it
returns can vary with which one is active.

Because the identity is not known until a connection exists, the cache is not
opened at app startup: `NivariaApp.on_mount` creates no `Repository` before
the connect dialog runs. It is opened once a pool exists
(`NivariaApp.switch_repo`), and re-opened by the same call after a role
switch or a reconnect. `MainScreen.set_repo` (and the matching method on
`ObjectTree` and `DetailPane`) is what repoints a widget at a different
`Repository` object entirely; it resets browsing state exactly like a fresh
connect would, since whatever was on screen was read out of a file that no
longer applies. The search modal needs no such method: it is built fresh
against whatever repository it is handed each time it opens.

The one sequencing rule this depends on: the running `FetchService` must be
stopped *before* `ConnectionPool.set_role` actually changes the role on every
connection, not after. A role switch reuses the same pool — only the
connections' session state changes — so a fetch worker still bound to the old
`Repository` that drew a connection after the role flipped would execute
under the new role but write results into the old role's file. Stopping fetch
first closes that window; `MainScreen._switch_role` does this, and restarts a
fresh `FetchService` against the new (or, on failure, the unchanged) repo
afterward.

### The freshness ledger and the pass protocol

One ledger row per scope records its state, generation, last completion time,
row count, last error, and whether the pass was **truncated**. This is what
makes a refresh *surgical* rather than "fetch everything again".

`truncated` is a third thing alongside COMPLETE and ERROR, not a shade of
either: the rows are real, current and swept in normally, there are simply
more of them upstream than the statement's own row cap asked for. It exists
because a history table has no natural bottom — it just stops — so a capped
one is indistinguishable from a complete one by looking at it, and presenting
a partial answer as a whole one is the thing this design does not allow.
`HistoryView` renders a note above the table when it is set, and `begin_pass`
clears it, so raising the cap (or a pipe going quiet) retires the note on its
own.

Refreshing a scope is a **pass**:

1. begin — claim the scope and bump its generation;
2. write — upsert rows, each stamped with that generation (repeatable);
3. finish — delete rows in this scope *older* than this generation, then mark
   it complete.

The rows count as fetched when the pass began (`attempted_at`, stamped by
`begin_pass`), not when it finished. A pass of many pages ends later than its
first page was asked for, and stamping the end would let those rows go stale
before the countdown said so.

Deletion only at the end is the point. Rows are never briefly absent, so the UI
never flashes an empty list on the way to a full one, and a chunked refresh is
safe to display while still running. An object that really was dropped
disappears exactly one pass later.

A write carrying a stale generation is rejected. Thread workers cannot be
interrupted, so a fetch that was already in flight when the user navigated away
is fenced on arrival instead.

A **failed** pass does not sweep. Showing yesterday's tables beats showing an
empty schema because the warehouse was briefly unreachable. Those rows would
then look as current as any others once the error toast is gone, so the notes
that report a failed first fetch (category lists, History, Data) also report a
failed refresh, with the time the rows on screen were fetched
(`tables.failed_refresh`).

#### Window passes

The sweep above suits a source that can change in any way: a `SHOW` shows a
dropped object only by its absence, so only a full answer can retire a row.
The four `ACCOUNT_USAGE` scopes are different. `PIPE_ACTIVITY`,
`PIPE_ACTIVITY_HISTORY`, `TASK_ACTIVITY` and `TASK_ACTIVITY_HISTORY` mirror
logs of past events, and a row there does not change once it appears:
`TASK_HISTORY` lists only finished runs (see
[snowflake-behavior.md](snowflake-behavior.md#task-history)). Their full
statement costs what its time window costs, not what it returns (8-31 s for
90 days of copy history on production, about 2 s for 2 days, see
[measurements.md](measurements.md#account_usage-windows)). So a refresh of
one of these **log scopes** asks only for what is new: a **window pass**.

`repository.plan_window` decides, from the ledger, how far back a pass asks:

- A **full pass** covers the whole fetch window. It runs the first time, when
  the ledger's `covered_from` (how far back the rows are complete, set by a
  full pass) no longer reaches as far as the fetch window because the setting
  was raised, after `Repository.forget_coverage`, and when the window would
  be at least as long anyway. It is swept like any other pass.
- A **window pass** covers the time since the last successful pass plus a
  **lag margin**. `ACCOUNT_USAGE` publishes a row some time after its event
  but stamps it with the event's time, and the window filters on that time.
  A row published after the window has moved past its time would never be
  asked for again, so each window reaches back by the margin: a multiple of
  Snowflake's published lag (~2 h for copy history, ~45 min for task
  history), set by the "lag margin" setting, 2x by default. A failed pass
  leaves `fetched_at` where it was, so the next window reaches back to the
  last success by itself.

A window pass is responsible only for what its window supersedes, so
`finish_pass` sweeps nothing it did not return. Instead it removes rows older
than the fetch window, keeps only the newest row per pipe or task for a scope
reduced to one (`LogShape.latest_per`), and keeps a capped history's newest
`HISTORY_MAX_ROWS`. A window pass that hit its own cap is swept like a full
one: its rows are then the newest overall, and keeping older ones would leave a
gap of rows it was not allowed to return. Times are compared with sqlite's
`julianday()`, never as text, because they are stored with the session's
offset.

Task scopes filter on `COMPLETED_TIME`, not `SCHEDULED_TIME`: a run is listed
only once it has finished, so a five-hour run appears carrying a scheduled
time five hours old, outside any window but the full one. Their key is
`(SCHEDULED_TIME, ATTEMPT_NUMBER)`, not `QUERY_ID`, which is NULL for a run
that failed before a query was submitted. NULLs never collide in a key, so an
overlap read again would add such a run once per pass.

`MainScreen.full_refresh` forgets the coverage of the log scopes on screen
and asks for them at once, so a row the margin missed can be recovered. It
has no key or menu item yet.

The cache is an optimisation, never a dependency: if it cannot be opened the
app still runs, it just has nothing cached. It uses WAL journalling so a
writing worker never blocks the reading UI, and opens a connection per call
rather than holding one, so the same code is usable from the event loop and
from worker threads.

### A schema version, and no migrations

`schema.sql` and `search.sql` are all `CREATE ... IF NOT EXISTS`, so an edit to
either never reaches a cache file that already exists — the first write to an
added column would simply fail. Every edit therefore bumps `SCHEMA_VERSION` in
`cache/db.py`, which is stamped into the file as sqlite's `user_version`. On
open, a file carrying any other version is deleted together with its `-wal`
and `-shm` and rebuilt empty. nivaria is pre-1.0 with no
backward-compatibility policy, so nothing is migrated; the cache refills
itself like a first start. A test pins a digest of both scripts to the
version, so changing a script without the bump fails the suite. The digest
covers the whole text, comments included.

Adding a column to an existing mirror table in place is therefore an ordinary
change, not an exception.

### Table data never reaches the disk

Everything above is metadata, with one exception: the Table and View views'
Data tab (`DATA_PREVIEW`) is real rows, and they can be personal data. They still go
through sqlite like any other scope, so the one rule holds and the pass
protocol, fencing and cursor keeping all apply. But they are kept in an
in-memory sqlite database (`MemoryDatabase`), one per `Repository`, never in
the cache file. `Repository` picks the database by scope type, so no caller
knows the difference. The memory database has its own `cache_entry`, so a
preview's freshness lives and dies with its rows. Previews are gone when the
app exits, and a reconnect or role switch starts without them.

Keeping previews in the file and deleting them is not enough. SQLite leaves
deleted content in free pages and in the `-wal` file until something
overwrites it, so the rows would stay recoverable from disk after nivaria
could no longer read them. The ledger entry would have to be deleted alongside
the rows, too, or it would read as a preview fetched and found empty.

The memory database breaks the cache's "a connection per call, never held"
rule, and has to. An in-memory database belongs to the connection that
created it and vanishes when that connection closes, so each call would get
an empty one. `MemoryDatabase` holds one connection for its lifetime, shared
by the event loop and the fetch workers under a lock, with no WAL. That
suits a few hundred rows, written once per fetch and read once per repaint.
A `TEMP` table on a held connection to the cache file was rejected. Temp
tables live in a temporary file on disk unless `temp_store` is set to
memory. Their ledger entries would have to go into the file's `cache_entry`,
which is the wipe problem again, or into a temp `cache_entry` that shadows
the real one. And a held connection keeps the cache file open, which on
Windows blocks deleting it on a `SCHEMA_VERSION` bump. Their one advantage,
joining previews with cached metadata in SQL, is not needed. What the
operating system does with process memory (swap, crash dumps) is out of
reach either way.

The preview table lives in `preview.sql`, which only the memory database
applies, so it is not part of the cache file's `SCHEMA_VERSION`. The memory
database applies `schema.sql` too, for `cache_entry`: the ledger code reads
both databases, so both must have the same table. The file's mirror tables
come along and stay empty.

The mirror cannot follow "Shape: mirror Snowflake", because every table has
its own columns. `data_preview` is generic instead: one row per table row,
holding the row as a JSON object keyed by column name in `SELECT *` order.
Column names are unique within a table, so nothing is lost. The grid's header
is read from the first row rather than from the cached column list, which can
be older than the rows and name other columns. JSON has no type for some of
what the driver returns, so a `Decimal` is stored as a string (a float would
drop a `NUMBER(38,x)`'s digits), a timestamp as ISO 8601, and a `BINARY` as
upper-case hex, Snowflake's own default output format. A `Decimal` with a
fractional part is written fixed-point, as `TO_VARCHAR` writes it; `str()`
would spell -0.0000000001 as `-1E-10`. Anything else the encoder does not
know is stored as `str()`, so a surprise degrades a cell's rendering rather
than failing the fetch.

The driver hands VARIANT, OBJECT, ARRAY, GEOGRAPHY and GEOMETRY over as
indented JSON text (see
[snowflake-behavior.md](snowflake-behavior.md#result-types)). The gateway
drops the whitespace between tokens for those columns, picked by their
`cursor.description` type code, because a Data cell is one line of 40
characters and the indentation alone would fill it. It scans rather than
parsing and re-serialising: a round trip would turn a 30-digit number into a
float and fail on the `undefined` an ARRAY holds for a SQL `NULL`. A VARCHAR
that only looks like JSON is the user's text and is left alone.

---

## Fetching: what, when, and how

### `SHOW`, because it needs no warehouse

All tree data comes from `SHOW` commands, and a table's columns come from
`SHOW COLUMNS`. This is a load-bearing choice, not a stylistic one:

- `SHOW` is served by Snowflake's metadata layer and **requires no running
  warehouse**. The reference account runs with no warehouse at all, and
  everything works. It also costs no credits and is unaffected by a suspended
  warehouse.
- `INFORMATION_SCHEMA` is an ordinary `SELECT` and therefore needs a warehouse.
  It would make the entire detail pane dead in a no-warehouse session.

`SHOW COLUMNS` returns a different shape from `INFORMATION_SCHEMA.COLUMNS`: no
ordinal position (columns arrive in declaration order, so position is derived
from that order, restarting per table), and the data type arrives as a JSON
blob that is unpacked into type/length/precision/scale. The cache keeps
`INFORMATION_SCHEMA`'s vocabulary because it is the stable, documented name for
each field — only the source changed. That holds for `DATA_TYPE`'s values too:
SHOW's JSON reports two internal categories, `FIXED` and `REAL`, which are
stored as the `NUMBER` and `FLOAT` that `INFORMATION_SCHEMA` reports. `FIXED`
is not valid DDL, so keeping it would put a type name in the cache that no
Snowflake statement accepts. A live comparison of all three sources (SHOW,
`INFORMATION_SCHEMA`, `DESC TABLE`) found no other differences in the type
name. A type that cannot be parsed degrades to
"unknown"; a column nivaria cannot describe is still a column worth listing.

What `INFORMATION_SCHEMA.COLUMNS` cannot do is describe a type in full. It has
no field for a BINARY's length (every length field is NULL for it), DECFLOAT's
fixed precision of 38, a VECTOR's element type and dimension, or a structured
type's elements and fields. SHOW's JSON carries all of that, so the mirror
gains one column of its own: `_desc_type`, the type exactly as `DESC TABLE`
prints it (`NUMBER(38,0)`, `VECTOR(INT, 768)`, `OBJECT(a VARCHAR(16777216)
NOT NULL, b FLOAT)`). The underscore marks the column as nivaria's, not
Snowflake's, because no Snowflake field holds this value, but the rendering
itself is Snowflake's. The column list shows it as Type. The rendering rules
come from live `DESC TABLE` output, and `tests/data/show_column_types.json`
keeps that output (51 declared types) as the test that holds them. The
`INFORMATION_SCHEMA` fields stay faithful next to it: BINARY's length goes
only into `_desc_type`, never into `CHARACTER_MAXIMUM_LENGTH`.

### A second source: one new mirror table per SELECT

`SHOW` remains the strong default above: it needs no warehouse and covers
almost everything the tree and detail pane show. But some facts genuinely do
not exist in any `SHOW` output — a table's last-DDL timestamp is only in
`INFORMATION_SCHEMA.TABLES`, a pipe's load history only in `COPY_HISTORY`.
Rather than avoid those features, or make the whole detail pane depend on a
warehouse, **each such fact gets its own scope, needing its own privileges,
degrading on its own.**

That is free, and it is what makes this a rule rather than a series of
special cases: a SELECT-backed scope is an ordinary `Scope`/`TableSpec`, so
`begin_pass`/`write_page`/`finish_pass`/`fail_pass`, the staleness sweep and
"a failed pass does not sweep" all apply unmodified. A failed or missing pass
renders those columns blank while everything `SHOW`-sourced in the same view
keeps showing yesterday's data. It gets no special treatment from the sweep
either: a scope on screen is refreshed on the same TTL as everything else,
warehouse or not.

Each new source has its own mirror table, named and shaped like the view it
reads (upper-case columns for an `INFORMATION_SCHEMA`/`ACCOUNT_USAGE` source —
the same exception `information_schema_columns` established).

#### The second-source scopes

| Scope | Source | Feeds | Needs |
| --- | --- | --- | --- |
| `TABLE_STATS` | `INFORMATION_SCHEMA.TABLES`, `TABLE_TYPE = 'BASE TABLE'` | Tables (n): Last DDL, Last Altered | warehouse |
| `VIEW_STATS` | the same statement, `TABLE_TYPE = 'VIEW'` | Views (n): Last DDL, Last Altered | warehouse |
| `MATERIALIZED_VIEW_STATS` | `SHOW MATERIALIZED VIEWS` | Views (n): Rows, Size | — |
| `PIPE_ACTIVITY` | `ACCOUNT_USAGE.COPY_HISTORY`, latest row per pipe | Pipes (n) activity columns; target-table resolution | warehouse, `IMPORTED PRIVILEGES` |
| `PIPE_ACTIVITY_HISTORY` | the same view, one pipe, unreduced | Pipe view History (older half) | warehouse, `IMPORTED PRIVILEGES` |
| `PIPE_HISTORY` | `INFORMATION_SCHEMA.COPY_HISTORY` | Pipe view History (recent half) | warehouse, a resolved target table |
| `PIPE_STATUS` | `SYSTEM$PIPE_STATUS`, one call per pipe, batched | Pipes (n) Execution State | a fetched pipe list |
| `TASK_ACTIVITY` | `ACCOUNT_USAGE.TASK_HISTORY`, latest row per task | Tasks (n): Last Scheduled, Last Run State | warehouse, `IMPORTED PRIVILEGES` |
| `TASK_ACTIVITY_HISTORY` | the same view, one task, unreduced | Task view History (older half) | warehouse, `IMPORTED PRIVILEGES` |
| `TASK_HISTORY` | `INFORMATION_SCHEMA.TASK_HISTORY` | Task view History (recent half) | warehouse |
| `GRANTS` | `SHOW GRANTS ON SCHEMA`/`ON DATABASE`/`ON TABLE`/`ON VIEW` | Schema/Database/Table/View view Grants | — |
| `TAG_REFERENCES` | `INFORMATION_SCHEMA.TAG_REFERENCES` | Schema/Database view Tags | warehouse |
| `COLUMN_TAG_REFERENCES` | `INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS` | Columns view Tags column + detail panel | warehouse |
| `DATA_PREVIEW` | `SELECT * FROM <table or view> LIMIT n` | Table/View view Data | warehouse |

The four `ACCOUNT_USAGE` scopes are log scopes, refreshed by window passes
(see "Window passes").

Two of these are second sources without being `SELECT`s:
`MATERIALIZED_VIEW_STATS` and `GRANTS` are additional `SHOW` commands, so
they need no warehouse and degrade the way any `SHOW` scope does. `SHOW
MATERIALIZED VIEWS` is genuinely a different command from `SHOW VIEWS`, with
its own column set carrying `rows`/`bytes` that `SHOW VIEWS` never returns —
which is also why a regular view's Rows/Size are blank, and why the Views (n)
list shows a Materialized column at all: it is the only thing that explains
the blank.

`TAG_REFERENCES` is `INFORMATION_SCHEMA.TAG_REFERENCES`, deliberately not
`SHOW TAGS IN SCHEMA`: that lists tag *objects defined inside* a container,
not tags *applied to* it. `ACCOUNT_USAGE.TAG_REFERENCES` was rejected too —
it needs `IMPORTED PRIVILEGES` and lags hours, for no gain over the
`INFORMATION_SCHEMA` view on a single object.

`COLUMN_TAG_REFERENCES` reads a different function,
`INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS`, not `TAG_REFERENCES` called
once per column: it returns every tag on every column of one table in a
single call, so the Columns view's Tags column costs one query per table
regardless of how many columns it has — the same "no query whose cost scales
with row count" rule the rest of this app already holds `SHOW` to. It filters
out `APPLY_METHOD = 'INHERITED'`, because the function also reports every
schema- and database-level tag against each column underneath (see
[snowflake-behavior.md](snowflake-behavior.md#tag-references)), and a column
must not show its schema's tags as its own.

Both functions report a multi-value tag (`MULTI_VALUE = TRUE`) as one row
per value, so `TAG_VALUE` is part of both mirrors' keys. Without it, the
cache's upsert would keep only the last value of each tag. The Columns view's
detail panel joins one tag's values onto a single `TAG = a, b` line.

Snowflake's own visibility rules still apply on top: `SHOW GRANTS ON <object>`
only returns what the active role owns, has `MANAGE GRANTS` on, or reaches
via `ACCOUNTADMIN`/`SECURITYADMIN`, so a short or empty Grants tab under a
lesser role is correct, not an incomplete fetch. It also lists **direct**
grants only: a role that holds a privilege because another role was granted
to it does not appear. The Grants tab answers "who was granted what on this
object", not "can role X read it".

A table's or view's grants are the same `GRANTS` scope narrowed to one
object (`table` holds its name and `object_type` its kind, since the
statement needs the noun -- `ON TABLE` or `ON VIEW`), written to the same
`show_grants` mirror under its own `_scope`. Unlike everything else a table
shows, there is **no batch form**: `SHOW GRANTS ON TABLE` takes one table,
and neither alternative fits -- `SHOW GRANTS TO ROLE` is grouped by role,
not by schema, and `ACCOUNT_USAGE.GRANTS_TO_ROLES` is rejected for the same
reasons as `ACCOUNT_USAGE.TAG_REFERENCES` above. So every table or view is
its own round trip, and the Table and View views ask for it only while
their Grants tab is shown (see "What decides a view's shape"). A
materialized view is listed by `SHOW VIEWS` (`is_materialized = true`) and
so opens as a view, and `ON VIEW` covers it (see
[snowflake-behavior.md](snowflake-behavior.md#show-grants)).

`PIPE_STATUS` has no batch form either, and is a function rather than a
`SHOW`: `SYSTEM$PIPE_STATUS` takes one pipe, as a constant, so no query over
a pipe list can call it. A pass names each pipe from the cached `SHOW PIPES`
rows and joins one call per pipe with `UNION ALL`, in batches of
`PIPE_STATUS_BATCH`, each batch one page of the pass. That saves the round
trips but not Snowflake's time per pipe (see
[measurements.md](measurements.md#pipe-status)), which is why the list asks
for it only while its Status tab is shown. One pipe that no longer exists, or
that the role may not see, fails a whole batch at compile time, so that batch
is asked for again one pipe at a time and the pipe is left out: its cell
stays blank until the next pass of the pipe list drops it. A batch in which
no pipe answers fails as "does not exist", so a dropped schema still reaches
the fetch layer's self-healing.

#### One row per pipe, in SQL

`PIPE_ACTIVITY` is reduced to each pipe's latest load by a `QUALIFY
ROW_NUMBER() OVER (PARTITION BY PIPE_NAME ORDER BY LAST_LOAD_TIME DESC) = 1`,
and that `QUALIFY` is load-bearing. Without it, on an account with ~400 pipes
and frequent ingestion, the query returns one row per load, over the whole
fetch window, across every pipe in the schema — hundreds of thousands of rows
(see [measurements.md](measurements.md#history-volume)) — and the app hangs
for minutes materialising them just to throw away all but the newest per pipe.
Collapsing in Python is not enough; it has to happen in SQL. `TASK_ACTIVITY`
carries the same reduction for the same reason.

`ACCOUNT_USAGE.COPY_HISTORY` is what the schema-wide list reads at all
because `INFORMATION_SCHEMA.COPY_HISTORY` requires a `TABLE_NAME` argument
with no schema-wide form — one round trip per distinct target table every
time the list opens, against the round-trips-dominate principle below.

#### Why a per-object history is a separate scope, and a separate table

A single pipe's or task's own history needs more than the latest row, so it
does not share the schema-wide scope: `PIPE_ACTIVITY_HISTORY` and
`TASK_ACTIVITY_HISTORY` query the same views filtered to one object,
**fetched lazily, only once that object's detail view is open** — a schema
full of pipes never pays for history nobody has looked at.

They get their own mirror tables rather than reusing the schema-wide ones
under a different `_scope` value, because those tables' primary keys do not
include `_scope`: a row visible to both would let whichever pass wrote it last
steal its `_scope` tag from the other.

#### Merging the two halves of a history

A pipe's or task's history merges an `ACCOUNT_USAGE` backfill with a
near-real-time `INFORMATION_SCHEMA` source, the latter inserted last so it
wins on overlap. Deliberately **not** a date-based split ("recent from one,
older from the other"): that would hide real data whenever one source is thin
or has failed outright, whereas overlaying needs no reasoning about which
scope succeeded.

The merge key differs, and the reason is Snowflake's. A task run is keyed on
`(SCHEDULED_TIME, ATTEMPT_NUMBER)`, the same key its mirrors use. `QUERY_ID`
looks like the natural key and is not: a run that `INFORMATION_SCHEMA` lists
as `SCHEDULED`, or one that failed before a query was submitted, has none, so
every such run would collapse into one row. Both sources carry the same
`SCHEDULED_TIME` for a run (see
[snowflake-behavior.md](snowflake-behavior.md#task-history)): it is the
planned instant, not a time each view stamps for itself. A pipe's file load
has no such column, so it is keyed on `(FILE_NAME, PIPE_RECEIVED_TIME)`.

`LAST_LOAD_TIME` looks like the natural second half of that key and is not:
the two views stamp it independently, so the same load carries slightly
different values in each (see
[snowflake-behavior.md](snowflake-behavior.md#copy-history)) and every load in
both sources would show up twice — hidden by the minute-precision "Loaded"
column. `PIPE_RECEIVED_TIME` is equal in both. A row without one falls back to
`LAST_LOAD_TIME`, so it cannot collapse several loads of one file name into
one.

Rows older than the "display window" setting are filtered out of the *view*
only; they stay in the fetch-window-bounded cache, so shrinking the display
window later needs no refetch.

#### Saying what is known while the backfill runs

The near-real-time half answers in a fraction of a second, the `ACCOUNT_USAGE`
half in seconds (see
[measurements.md](measurements.md#history-latency)). A blank table under the
busy indicator for that long reads as slower than Snowsight, so until both
halves have answered, the note above the table says what the answered ones
cover: "No run history in the last 7 days. Still looking...", or "Showing the
last 14 days. Still looking for older load activity..." when rows are already
there.

Only the near-real-time half makes such a claim. `ACCOUNT_USAGE` lags by hours,
so the backfill alone cannot speak for the most recent loads or runs, however
far back it reaches. A near-real-time half that covers the whole display
window is the full answer, and the note says so without waiting for the
backfill.

"Answered" means fetched at least once, or failed. A background refresh keeps
the cached rows on screen and is not waited for, so revisiting a history stays
as quiet as before; the note only speaks during a first fetch.

#### Resolving a pipe's target table, and the pipe that never loaded

`INFORMATION_SCHEMA.COPY_HISTORY` wants a `TABLE_NAME`, even when given a
`PIPE_NAME`, and Snowflake keeps a pipe's target table in no named field (see
[snowflake-behavior.md](snowflake-behavior.md#pipe-target-table)).
`Repository.pipe_target_table` therefore resolves it in this order:

1. Whatever `PIPE_ACTIVITY` has cached for the pipe: Snowflake's own
   resolution of its newest load, already fetched for the Pipes (n) list.
   That is an opportunistic read of a sibling scope's cache, not a
   dependency between scopes.
2. Otherwise the newest load in the pipe's own `PIPE_ACTIVITY_HISTORY` that
   names a table: the same resolution. A pipe view opened before the
   schema-wide snapshot has answered gets it sooner this way (see "The pipe
   the snapshot shows no load for").
3. Otherwise the target named in the pipe's cached `SHOW PIPES` definition,
   **only when it is fully qualified** (`pipe_definition.copy_target`).
4. Otherwise none.

The third step is the one place nivaria reads a fact out of SQL text rather
than a named field, and it exists because the alternative is worse. A pipe
with no load in the fetch window has no `PIPE_ACTIVITY` row, so without the
definition its near-real-time 14-day query would not run, and its first load
would show only once `ACCOUNT_USAGE` published it, up to hours later.

Only the start of the statement is read: `COPY INTO` and up to three
identifier parts, tokenized by Snowflake's documented rules for unquoted and
quoted identifiers and comments. What follows does not matter. A full SQL
parser was rejected for exactly that: it parses the whole statement, so an
option it does not know loses the target too, and `sqlglot` fell back to an
unparsed command on `ON_ERROR = SKIP_FILE_10%`, a documented value. Only a
fully qualified name is accepted because Snowflake resolves an unqualified or
schema-qualified target against the session's schema at `CREATE PIPE` time and
stores the text as written. The pipe's own schema is a guess that can name a
missing table, or a same-named table that is the wrong one.

A pipe with no target from either source is a normal, lasting state, not a
transient failure to retry. Queuing `PIPE_HISTORY` anyway would make
`fetch_page` raise and the fetch layer report an error on every attempt,
forever. So the gate lives above the fetch layer: `PipeView.scopes()` only
lists `pipe_history_scope` once a target actually resolves, and
`update_content` only `ensure()`s it under the same condition — so neither the
interactive path nor the background sweep (which watches `scopes()` too) ever
queues it. `fetch_page` still raises if `PIPE_HISTORY` is requested with no
target: a safety net for a caller that skips the gate, not the expected path.

Such a pipe is only covered by `ACCOUNT_USAGE`. Its snapshot is refreshed on
the cache age like everything else, and its first load gives it a target, but
that load shows only once `ACCOUNT_USAGE` has published it. That delay is not
silent: its History tab carries the same muted lag line as the Pipes (n)
Activity tab and the Tasks (n) list (`tables.lag_note`), worded after
Snowflake's "up to" figure so it promises no maximum. A pipe with a known
target does not show it: its recent days come from `INFORMATION_SCHEMA`.

A resolved target can still fail: its table may have been dropped, or the
role may not see it. `PIPE_HISTORY` then fails with "does not exist", which
here is about the table, never the pipe: the statement names the pipe only as
a filter value. So it is treated as a lasting state, not a stale listing. It
refreshes no pipe list (`containing_scopes` gives none) and raises no toast.
The History note says which part could not be read, in Snowflake's own words,
and the lag line shows, since only `ACCOUNT_USAGE` is read. The query is
retried quietly every cache age, a fraction of a second each, so a re-created
table or a new grant is picked up by itself.

The note is the general rule for a history half that fails while the other
has rows (`HistoryView.sync_history_note`): those rows would otherwise look
like the whole history. It names the missing part, "The last 14 days of load
activity" or "Older run history", and why.

#### The pipe the snapshot shows no load for

`PIPE_ACTIVITY` keeps each pipe's latest load in the fetch window. A fetched
snapshot without a row for a pipe therefore says the pipe loaded nothing in
that window, up to the snapshot's `fetched_at` minus `ACCOUNT_USAGE`'s lag
(`Repository.pipe_idle_as_of`). The pipe's own `PIPE_ACTIVITY_HISTORY` reads
the same view over the same window with the same lag, so it could only repeat
that answer, at about 6.5 s per pipe (see
[measurements.md](measurements.md#history-latency)). So once the snapshot
has answered **without** a load for the pipe, `PipeView` no longer asks for
it (`PipeView.needs_backfill`).

Until the snapshot has answered, the backfill is asked for alongside it, not
after it. On a busy account the schema-wide snapshot takes up to half a
minute, one pipe's backfill a few seconds (see
[measurements.md](measurements.md#account_usage-windows)). And for a pipe
whose definition does not name its target in full, the backfill is also what
first names the table the 14-day history needs. Waiting would leave a pipe
opened straight from search, or after a cache file was replaced, empty for
that long. The price is one backfill for a pipe that turns out idle, paid
once, since later refreshes are window passes. A snapshot whose only attempt
failed stops it as well: the backfill reads the same view and would fail the
same way.

For such a pipe the snapshot is the history's older half, and the 14-day
`PIPE_HISTORY` its recent half. The snapshot is refreshed by a window pass on
the cache age, so it is never more than one cache age plus `ACCOUNT_USAGE`'s
lag behind, and the two join without a gap. The note treats the snapshot as a
lagging source that claims nothing early, so the answer is complete once the
14-day history has answered, a fraction of a second after the view opens.

`TaskView` needs none of this. `INFORMATION_SCHEMA.TASK_HISTORY` accepts
`TASK_NAME`/`DATABASE_NAME`/`SCHEMA_NAME` as direct named arguments, all of
which a task's own `ObjectRef` already carries, so both its history scopes
are always requested once the view is open.

One thing that looks redundant and is not: `queries.task_history` still
database-qualifies the function name (`"<database>".INFORMATION_SCHEMA.
TASK_HISTORY(...)`) even though `DATABASE_NAME` is also one of its named
arguments. Those are two different things: an unqualified
`INFORMATION_SCHEMA.TASK_HISTORY` resolves against whichever database is
current in the session, which the pool never sets (only `USE ROLE`/`USE
WAREHOUSE` are ever issued), so it fails with "invalid identifier" regardless
of where the task lives (see
[snowflake-behavior.md](snowflake-behavior.md#task-history)) — silently, since
the `ACCOUNT_USAGE` half of the merge still returns data.
`queries.pipe_history` qualifies `COPY_HISTORY` the same way.

#### Staleness is decided in one place

Every scope ages on the same `CACHE_TTL`. A scope whose full statement is
expensive is not refreshed less often; it is made cheap to refresh instead
(see "Window passes"). A longer cache age would save the statement, but
everything the scope feeds would then be out of date by that much.

**`models.due_in(age, ttl)` is the one place staleness is decided anywhere
in the app.** The sweep and the footer countdown (`Repository.stale_scopes`
and `next_due`) reach it through `Entry.due`, which also knows the ledger
states (see "The staleness sweep"). The paint-time checks go through
`models.is_stale`, which is `due_in` reaching zero: `_ScopedView.ensure` (a
detail view's) and `ObjectTree._ensure` (the tree's). Having one function is
not tidiness: separate checks drift, and a tree that snapshots `CACHE_TTL` at
construction ignores a settings change until restart.

`object_type` on a `Scope` means "this is a `SHOW` listing of one object
type" -- or, on an object-level `GRANTS` scope, which kind of object it names
-- and nothing keys off it being set: `MainScreen.on_scope_needed` upgrades a
scope to a whole-database `SHOW`-prime when its *type* is `OBJECTS`, not when
it happens to carry both a database and an object type. A structural check
would silently redirect any new SELECT-backed scope that sets `object_type`
into fetching `SHOW TABLES` instead of its own statement; keyed on the type, a
new scope does not have to know.

### Round trips dominate; payload is nearly free

A `SHOW` round trip costs about the same regardless of how many rows it
returns (~187 ms against the reference account, see
[measurements.md](measurements.md#round-trips)).

This inverts the intuitive approach to chunking. Small pages do **not** make
progress feel smoother — they multiply latency. Pages exist to **bound a job
and give cancellation points**, not to animate progress. Lowering the page
size setting mainly makes things slower.

### Database priming

Because round trips dominate, a schema's objects are **not** fetched per
schema. One `SHOW <type> IN DATABASE` returns every schema's objects at once,
about ten times faster than one call per schema on a 54-schema database (see
[measurements.md](measurements.md#database-priming)). Any request for one
schema's objects is therefore upgraded to a database-wide prime, which then
completes *every* schema's scope from that single result.

Schemas that returned nothing still get a completed pass. Otherwise they would
read as "never fetched", the tree would keep asking, and the node would never
settle.

> **Do not add keyset paging to the database-wide prime.** Object names are not
> unique across schemas — an `ORDERS` table in forty schemas is forty rows
> sharing one cursor value — so resuming after a name silently skips the rest
> of them. The prime therefore issues one unpaged statement and, if the result
> comes back at or over `PRIME_CHUNK_THRESHOLD`, falls back to fetching schema
> by schema. Within a *single* schema, where names are unique, keyset paging is
> verified correct and is used — for the object types that honour it.

The threshold means "this response is too big to take in one bite", not
"Snowflake truncated us at 10 000": a `SHOW` result at the threshold may or
may not be complete — `SHOW` can return more than 10 000 rows (see
[snowflake-behavior.md](snowflake-behavior.md#show-row-counts)) — and either
way the answer is the same: fall back to a chunk small enough to trust whole.

### Which SHOW commands page

`LIMIT n FROM 'name'` keyset paging is verified correct for databases, schemas,
tables and views, and a confirmed no-op for `SHOW PIPES`, `SHOW COLUMNS` and
`SHOW STAGES`, at both `IN SCHEMA` and `IN DATABASE` scope. `SHOW TASKS`
honours `LIMIT`, but its `FROM` is inclusive: a page starts with the cursor
row itself. The observations are in
[snowflake-behavior.md](snowflake-behavior.md#keyset-paging).

`jobs.fetch_page` absorbs the inclusive `FROM` for every pageable type rather
than special-casing tasks. A continuation page asks for one row more than the
page size and drops a first row whose name equals the cursor. Exclusive
types never produce that echo, so for them the spare row is simply one more
row. Without the spare, a page size of 1 would come back as the echo alone,
the cursor would not move, and the stuck-cursor guard below would end the
pass after the first task. The spare row is kept rather than trimmed:
trimming would also trim the page of a statement that ignores `LIMIT`, which
that guard then takes for the whole result.

`ObjectType.pageable` is where that finding lives in the code, and the
statement builders consult it: `show_objects`/`show_objects_in_database` emit
no paging tail at all for an unpageable type, and `show_columns` takes no
`after`/`limit` arguments. A clause
Snowflake ignores is worse than no clause, because the SQL log then shows a
bound that was never applied.

`jobs.fetch_page` returns no cursor for an unpageable type, so one call is the
whole result and no continuation is ever queued. That also means **truncation
is undetectable for those statements** — without a working `LIMIT` there is no
row count that can mean "there was more" — which is why the row ceiling below
is their only bound.

`MAX_PAGE_SIZE` is documented as the largest `LIMIT` worth *asking* for, never
as a ceiling anything may rely on: Snowflake's own "SHOW caps at 10k rows" does
not hold universally.

#### Why a paging no-op is a hang, not a slow fetch

Continuation decides "there is another page" from `len(rows) == limit` and
resumes with `FROM '<last row's name>'`. Against a statement that ignores both
clauses, every "next page" comes back as the identical full result with the
identical last name: **an unbounded loop, not merely a slow one.** The pass
never reaches `finish_pass`, and a worker thread plus a pool connection stay
pinned re-issuing the same query. The default pool is three workers, so three
such scopes wedge the whole app. That shape is why the guards below exist
rather than a one-line fix.

Two paths reach the paged path for an unpageable type, and the second is the
one that matters:

- `_fan_out`, after a whole-database prime comes back over the chunk
  threshold, queues one per-schema `OBJECTS` job. It is object-type agnostic,
  so pipes and stages sit on it identically.
- **The staleness sweep** (`MainScreen._sweep`) requests
  `tree.visible_scopes()` *directly*, so it never passes through
  `on_scope_needed`'s `OBJECTS` → `DATABASE_PRIME` upgrade. An expanded Pipes
  or Stages node in a schema with more objects than `PAGE_SIZE` is enough on
  its own. Anything that bypasses that upgrade is on the paged path, and the
  sweep is not the only caller that could.

Lowering `PAGE_SIZE` below a schema's pipe count reproduces the loop, which is
also how the guards are verified.

### Bounding a result

Three layers, deliberately independent, because each covers what the others
cannot. Every one of them is a `Setting`, so someone who knows their account
can move it.

1. **Declare what cannot be paged** (`ObjectType.pageable`, above). Fixes what
   is known. Cannot cover a statement nobody has tested yet.
2. **Guard the continuation chain** (`FetchService._execute`, see "The fetch
   scheduler"). Catches an unpageable statement the builder did not know
   about, such as a type Snowflake adds later.
3. **A row ceiling in the gateway** (`ROW_CEILING`, `PoolGateway._collect`).
   The only bound that applies to every statement rather than to the ones
   somebody remembered to cap. It reads the cursor in batches and raises
   `ResultTooLargeError` past the ceiling, which routes through the ordinary
   worker boundary to `fail_pass`: no sweep, yesterday's rows stay on screen,
   the error is visible. Be clear about what it buys — it bounds what nivaria
   *materialises* and hands the connection back early. It does not stop
   Snowflake from having produced the result. `PoolGateway._fetch`'s blind
   one-shot retry re-raises it explicitly rather than repeating a statement
   that will return just as much the second time.

On top of those, the **history statements carry their own row cap**
(`HISTORY_MAX_ROWS`), because they are the only queries whose size follows how
*busy* an object is rather than how many objects exist. `pipe_activity` and
`task_activity` are `QUALIFY`-reduced to one row per object, but a *single*
pipe's `pipe_activity_history` is otherwise bounded by nothing except its
90-day fetch window, and a busy schema produces hundreds of thousands of raw
`COPY_HISTORY` rows in two weeks (see
[measurements.md](measurements.md#history-volume)). `ORDER BY <time> DESC
LIMIT n` is the fix, and the `ORDER BY` is half of it: without it a `LIMIT`
keeps an arbitrary slice, which is worse than no cap because it still looks
complete. `INFORMATION_SCHEMA.TASK_HISTORY`'s `RESULT_LIMIT` is exactly that
mistake on its own, with no documented ordering. It is still set, clamped to
the function's maximum, but the outer `ORDER BY ... LIMIT` is what decides
which rows survive.

`pipe_activity`/`task_activity` are deliberately left uncapped: a cap there
could only drop real pipes out of the "Pipes (n)" list, and layer 3 already
covers the pathological case.

The **data preview** carries its own cap too (`DATA_PREVIEW_ROWS`, 100 --
Snowsight's own Data Preview limit for both tables and views), and is the
one capped statement with no `ORDER BY`. That is the mistake described
above, made deliberately: ordering would make Snowflake scan and sort the
whole table to hand back 100 rows of it, and a preview promises no
particular rows. What makes the history mistake a mistake is letting an
arbitrary slice look complete, so the Data tab says so instead: a capped
preview reads "Showing 100 of n rows, in no particular order", with n from
`SHOW TABLES`' `rows` when cached. A view has no such count in `SHOW VIEWS`,
so its note just says "Showing 100 rows".

The preview is also the one statement with a **timeout**
(`DATA_PREVIEW_TIMEOUT`, 15 s), because its `LIMIT` bounds what a *view*'s
preview returns, not the work behind it. A view's query runs each time the
view is read -- its results are not stored -- and when that query
aggregates, deduplicates, uses a window function or sorts, Snowflake has to
see all of its input before the outer `LIMIT` can apply. Snowflake documents
the sorting case: with `ORDER BY` and `LIMIT` it "ordinarily scans all
eligible rows", short of narrow top-K pruning conditions. Snowsight bounds
its own view preview the same way, with a 300 s cancel, and warns that a
complex view may show none; 15 s is deliberately shorter, since the
Data tab follows the tree cursor and each preview holds a pool worker while
it runs. The worker thread still cannot be interrupted (see "The fetch
scheduler"). The driver can: `cursor.execute(..., timeout=n)` starts a timer
thread of its own that asks Snowflake to cancel the query, and `execute`
then raises errno 604 ("SQL execution canceled"), leaving the connection
usable (see [snowflake-behavior.md](snowflake-behavior.md#query-timeout)).
That is an ordinary failed pass, shown in the Data tab's note. Like any scope
whose first fetch failed, a view that always times out is tried again once per
sweep while its Data tab stays open. The timeout applies to tables too: one
statement, one setting, and a table preview that runs that long is just as
stuck.

> **Counts first was considered and rejected.** For a `SHOW` a count needs
> `RESULT_SCAN(LAST_QUERY_ID())`, an ordinary `SELECT` — so it needs a
> warehouse, breaking the rule the whole tree rests on, *and* the `SHOW` has
> already returned everything by the time it could run. For the history
> `SELECT`s a count costs the same scan as the query and doubles the round
> trips, in a design where round trips dominate. A bounded query gets the rows
> **and** the "was there more" signal in one trip (`len(rows) >= limit`) — the
> same trick `fetch_database`'s `usable` check already uses.

---

## The connection pool

In a corporate account **every new connection can trigger an interactive MFA
prompt**. The pool exists to make that a one-time event. What a user sees of
this is described in [OPERATIONS.md](OPERATIONS.md#connections-and-mfa); the
reasons are here.

- The connect dialog opens exactly **one** connection when submitted. There is
  no point asking about the rest before knowing that one even works, and a
  failed first connection is the common case the retry loop exists for.
- Growing the pool beyond one connection is **confirmed** in a dialog.
  Declining leaves the pool at size one rather than failing or silently
  upgrading — which is also what keeps a pool size of one always reachable on
  demand, handy under test and for a quick look that would rather not field
  extra MFA prompts. OAuth skips the question: its cached access/refresh
  token is reused for every connection the pool opens (see "Authenticators"),
  so growing the pool costs no further prompts and there is nothing to weigh.
- Confirmed growth, like the first connection, is opened **eagerly**, with the
  same progress lines whether it is the lone up-front connection or growth
  confirmed afterwards, always counted against the pool's eventual target
  size. The countdown starts at `CONNECT_TIMEOUT_SECONDS` (`gateway/pool.py`),
  the same value passed to the driver as
  `login_timeout`/`external_browser_timeout` — so a denied or abandoned OAuth
  or password+MFA approval resolves in a short, visible wait instead of the
  driver's undocumented multi-minute default. There is deliberately no Cancel
  button: `snowflake.connector.connect()` cannot be interrupted from the
  outside once called, so a Cancel button could only hide the dialog while the
  attempt kept running unseen underneath — see "Accepted design gaps" for the
  one authenticator this timeout does not actually bound.
- Every connection — the first and any confirmed growth — is opened
  **sequentially**. Opening them in parallel would fan out N browser windows or
  push notifications simultaneously — exactly the disruption this design
  exists to avoid.
- Sessions are kept alive, and the driver's SSO/MFA token cache is enabled, so
  the session does not expire mid-browse and re-prompt hours later, and
  connections 2..N usually do not prompt at all.
- Sessions identify themselves with the driver's `application` kwarg
  (`nivaria`), not a `QUERY_TAG`: a session-level tag silently overrides any
  tag an admin set on the user or account, which would break their cost
  attribution for every nivaria query.
- A worker that needs a connection while all N are busy simply blocks. That is
  the "stalled worker" state and it needs no special handling beyond the Pool
  gauge reflecting it.
- A connection found closed is transparently replaced rather than handed out
  — but only for an **interactive or visible-priority** caller, someone
  actively at the keyboard right now. A **background-priority** caller (the
  staleness sweep, which can run for hours with nobody watching) must never
  cause a new authenticated session to be opened unattended, since for
  `externalbrowser` that can mean a browser/MFA prompt appearing while the
  user is away. So a background job that draws a dead connection skips past
  it to whatever else is already idle instead — never waiting on one that is
  merely busy, only on one already known to be dead — and if nothing idle is
  alive, it fails fast rather than block or reconnect. Either way, the
  connection is closed and dropped from the pool rather than reused, so the
  **Pool gauge's total honestly shrinks** to match what is actually alive.
  The gap this leaves is only ever repaired by the next interactive/visible
  checkout that finds the pool short — it opens the missing connection
  itself, on demand, restoring the total. A connection that fails only once
  actually queried (the case a closed-check at checkout cannot see) follows
  the same rule: replaced transparently for an interactive/visible caller,
  retried once against the fresh connection; dropped and shrunk, with no
  retry, for a background one.
- **"Does not exist" is the exception to that rule**: the connection that
  reported it is healthy, so `PoolGateway._fetch_once` hands it back to the
  pool and only then raises, and `_fetch` does not retry it. Otherwise every
  visit to an object dropped elsewhere would close two working connections
  and open two new ones, each a possible MFA prompt. A data preview cancelled
  by its timeout (errno 604, see "Bounding a result") is treated the same:
  cancelling one query leaves its session fine, and a retry would only run
  out the timeout again. Kept to those two error numbers on purpose: an
  expired session is the same driver exception type, and that one must still
  be discarded.

### Stragglers on quit and reconnect

`ConnectionPool.close()` -- called on quit and at the start of a reconnect,
right after `FetchService.stop()` -- only closes connections that are
currently idle. One a worker thread still has checked out is left alone: a
worker's `cursor.execute()` call cannot be interrupted (see "The fetch
scheduler"), so closing that connection from another thread races the
driver's own session teardown against whatever request is still in flight on
it, which can hang the whole app on quit until the query finally gives up.
The straggling connection is simply abandoned instead -- the process exiting
(or, on reconnect, garbage collection once nothing references it any more)
reclaims it for free, and nothing here waits on it.

The worker that straggles is dropped the same way: once `FetchService.stop()`
has run, whatever a worker still mid-query brings back -- rows or an error --
is neither written, announced nor reported, leaving the cache as a quit
mid-fetch does. Reporting it would turn a query that failed against the
just-closed pool of one session into an error toast in the session the user
has already switched to. Writing it is no safer: a role switch shares the
pool, so `PoolGateway._fetch`'s retry of a failed statement runs after `USE
ROLE` and would put the new role's rows into the old role's cache.

Quitting waits for stragglers rather than abandoning them by default:
`Ctrl+Q` with a statement in flight (`FetchService.running_count`) stops the
service and waits until the count drains, so every connection is idle and
actually closed. A stopped service claims no further queued job, so the count
can only fall. A second `Ctrl+Q` exits at once and leaves the rest to the
straggler rule above. The wait polls the count on the event loop, from a
worker: awaited in the action itself it would block the message queue the
second `Ctrl+Q` arrives on, and `asyncio.to_thread(fetch.stop, None)` would not
do either, because asyncio waits for its executor threads on the way out and a
forced quit would sit out the query anyway.

Fetch workers are not the only threads that can outlive the app: the connect
dialog's connection attempt, pool growth and a role or warehouse switch cannot
be interrupted either, and quitting does not wait for those. Each ends by
hopping back with `call_from_thread`, which Textual answers with "App is not
running" once the app is gone. `NivariaApp.call_from_thread` drops the call
instead when the app has stopped running: the result is for a UI that no
longer exists.

### Authenticators

Three authenticators are supported and chosen in the connect dialog: OAuth
(`oauth_authorization_code`, the default), browser SSO (`externalbrowser`),
and password + MFA (`username_password_mfa`). The password field is disabled
for SSO and OAuth, where a browser popup owns the credential and a typed
password would be silently discarded.

OAuth here means the driver's own authorization-code (PKCE) flow, not the
`oauth` authenticator that expects an already-obtained token pasted in --
there is deliberately no token field in the dialog. With no client ID or
authorization URL configured, the driver treats the Snowflake account itself
as the identity provider: `connect_all` opens a browser to it, completes the
exchange on a local redirect listener, and hands back an access token, the
same shape of interaction as browser SSO. `oauth_enable_refresh_tokens` is
set alongside it so the refresh token that comes back is cached (via the same
`client_store_temporary_credential` flag SSO and password+MFA rely on) and
spent silently on the next connection or session, instead of opening a
browser every time the access token expires.

> **This is why `snowflake-connector-python[secure-local-storage]` (the
> `keyring` extra) is a real dependency, not an optional nicety.** On Windows
> and macOS the driver's token cache is backed by the OS keyring; without the
> `keyring` package installed it silently falls back to a no-op cache that
> never persists anything, and the symptom is exactly the problem this
> architecture exists to avoid -- every connection in the pool, OAuth or SSO,
> reopens a fresh browser prompt instead of the second connection onward
> reusing what the first one just obtained. Linux instead falls back to a
> permissioned file under `~/.cache/snowflake/`, which needs no extra
> dependency.
>
> On Windows, installing `keyring` is necessary but not sufficient: its
> default backend (`WinVaultKeyring`) stores each token as a
> `CRED_TYPE_GENERIC` Windows Credential, and that credential type is capped
> by the OS at 2560 bytes regardless of how it is persisted. A Snowflake ID
> or MFA token is a short opaque server-side reference and fits; an OAuth
> access or refresh token is a real JWT and routinely does not, so
> `CredWrite` fails with a raw Win32 error (1783, "the stub received bad
> data") that neither `keyring` nor the driver's `KeyringTokenCache` catches
> -- both only trap `keyring.errors.KeyringError` -- so it surfaces to the
> connect dialog as a failed connection. `credential_cache.install()`
> (`gateway/credential_cache.py`), called from `pool._real_connector` before
> the driver ever touches its cache, swaps in a DPAPI-encrypted flat file as
> the active `keyring` backend on Windows only: DPAPI ties the ciphertext to
> the current Windows user profile the same way Credential Manager does, so
> this is not a step down in secrecy, and a flat file has no comparable size
> ceiling. macOS Keychain has no equivalent restriction, so this is
> Windows-only.

### Credentials and profiles

**nivaria stores no password anywhere.** Each remembered identity is a
`Profile` (`nivaria/profiles.py`, on disk at `recent.json`): account, user,
authenticator, role, warehouse and an optional display name, plus the
databases last selected for that identity's Object tree -- UI state riding on
the same record because it is naturally scoped to the same identity, which is
also why the type is more than a bundle of connection parameters. For
SSO and OAuth there is nothing to store -- the driver obtains and caches its
own credential; for password+MFA the driver's own token cache already means
the password is typed once per machine rather than once per connection.
Connections are recorded as "recent" only after they have actually worked --
but a database selection is written the moment the picker confirms one, live,
since there is no equivalent "did it work" gate for a UI preference the way
there is for a connection attempt.

The two writers own disjoint fields of the same record -- the connect screen
the connection fields and the name, the picker the databases -- so both go
through `ProfileStore.remember`, which merges into the stored record for that
identity instead of replacing it; rebuilding the record from the connection
parameters alone would wipe a profile's name every time its databases were
picked. Identity is account/user/authenticator, with account and user
casefolded because Snowflake resolves them case-insensitively. A `recent.json`
that exists but cannot be read shows as "no recents", but is never written
over: every write is a read-modify-write, and writing on top of that empty
read would replace every stored entry with one.

---

## The fetch scheduler

Background workers, one per pool connection. Nothing here touches a widget: a
worker writes into sqlite and then posts a message naming the scope it changed.

- **One job fetches one page**, and re-enqueues its own continuation. This
  bounds how long any job holds a connection, gives free cancellation points
  (a superseded job simply does not re-enqueue), and lets an interactive
  request overtake a long background sweep at the next page boundary. The
  database prime is the deliberate exception — see above.
- **Three priorities**: interactive (the user just expanded something or
  pressed refresh) > visible > background (the staleness sweep).
- **Deduplicated**: a scope already queued or running is not queued again. A
  higher-priority request for an already-queued scope supersedes it rather than
  duplicating it. This is what stops a burst of expand events queueing the same
  work five times.
- **Self-healing**: a job that fails with "does not exist" requests the
  listing one level up, at its own priority, so a node for something dropped
  elsewhere disappears straight away -- see "Scopes: the unit of
  everything".
- **Cancellable**: switching database drops queued work for the old one. A page
  already in flight cannot be interrupted, but its continuation is suppressed.
- **Guarded**: a continuation is only queued if the cursor actually *advanced*
  and the pass is still inside its page budget (`MAX_PAGES_PER_PASS`). The two
  guards reach different conclusions, and that difference is the point:

  - *Cursor did not move* → the page was the whole result, so the rows are
    complete and current. `finish_pass` runs normally, with a WARNING logged
    naming the scope. Failing here would throw away a good result to report a
    paging quirk the statement builder was supposed to have handled.
  - *Page budget exhausted* → the cursor keeps moving but the result will not
    end, so this one really is incomplete. `fail_pass`, which does not sweep,
    so what was on screen stays there and the error surfaces rather than a
    half-fetched scope looking finished.

  Neither is belt-and-braces. The chain re-issues a statement and trusts the
  result to shrink, which is exactly the assumption Snowflake has been caught
  breaking — see "Bounding a result".

Results reach the UI as a message naming **one scope**, which the main screen
**routes** to the widget showing it — never broadcasts. An update to one
schema's tables cannot cause a whole-tree rebuild.

### The staleness sweep

The footer countdown makes one promise: **nothing on screen goes stale before
it reaches zero.** "On screen" means the watched set: every scope the tree
has materialised, collapsed or scrolled away included, plus the open detail
view's. Every scope ages on the same cache age.

The countdown is therefore computed, not run as a timer. On every tick,
`Repository.next_due` works out from the ledger how long until the first
watched scope goes stale, and the countdown shows that. When it reaches zero,
`MainScreen._sweep` requests every watched scope that is stale, at background
priority. A stale schema listing is requested as its database prime, like any
other request for one (see "Database priming"). Those are then busy and drop out, so the countdown moves on to
whichever scope is due next: it jumps up after a sweep, and down when a view
shows a scope that is close to its TTL. Navigation and `F5` affect it only
through the ledger.

`F5` is the explicit refresh, outside the sweep. It requests the scope of the
tree node under the cursor, and every scope the detail pane reads, whatever
their age. A log scope's refresh is a window pass either way, so `F5` costs no
more there than the sweep would. A fixed timer restarted at `CACHE_TTL` cannot keep the
promise: a refresh lands a moment after the sweep that asked for it, so the
next zero comes just before that data goes stale, skips it, and leaves it on
screen for nearly two TTLs.

Zero means requested, not fresh. Until a refresh lands, its data is up to one
fetch's duration past its TTL, which is what the countdown's spinner shows.

**Busy** scopes, queued or mid-fetch, either themselves or through the prime
that fills them (`models.filling_prime`), are left out of both the countdown
and the sweep. A busy scope's old `fetched_at` reads as overdue until its pass
lands, and requesting a listing that a database prime is filling would be the
per-schema round trip the prime exists to replace.

A **failed** refresh (`ERROR`) ages from its last attempt (`attempted_at`)
rather than from its rows. `fail_pass` keeps the old `fetched_at`, so aged by
that a failed scope would read as overdue forever, and every sweep, whichever
scope triggered it, would rerun the failing statement. It is retried one TTL
after it failed, when the user navigates back to it, and at once after a
successful warehouse switch (`MainScreen._retry_failed`), since a missing
warehouse is the usual cause. `Entry.due` holds all of this, and
`stale_scopes` and `next_due` both read it, so the sweep and the countdown
agree on every scope.

With nothing left to count, the countdown shows the full TTL. Scopes nobody
is looking at are never refreshed this way; anything urgent was already
requested interactively on the way in (`_ScopedView.ensure`,
`ObjectTree._ensure`). Nothing else fetches in the background except the
initial requests `MainScreen.on_mount` issues at startup. A TTL of `0`
disables the sweep entirely.

A view's paint-time check never re-requests the scope whose own update it is
painting. A scope whose first fetch failed has no `fetched_at`, so it always
reads as stale, and re-requesting it from the repaint its failure caused
would make a failing statement (no warehouse, say) retry itself — looping or
not depending on whether the worker left `FetchService._running` before the
repaint ran.

---

## The Object tree

Lazy: a node's children are materialised when it is first expanded. Counts on
collapsed nodes (`Schemas (54)`, `Tables (383)`) come from the cache, so they
are correct and instant without materialising anything beneath them.

One top-level node per selected database. Several databases can be selected at
once (the Databases picker, `MultiPickerDialog`), so the tree carries no border
title naming one, and search runs across all of them.

### The cursor must not move

The hard requirement: an update never moves the cursor off what the user was
looking at. Reconciliation is a pure diff of the desired children against the
current ones, applied in three tiers:

1. **Nothing changed** → nothing is mutated. This is the overwhelmingly common
   outcome of a TTL refresh, and the cursor cannot move if nothing is touched.
2. **Removals, relabels, and additions that belong at the end** → applied in
   place.
3. **A name sorting into the middle** → the children are rebuilt.

The tiering exists because of a specific Textual property, which future work
must not assume away:

> Textual's `Tree` keeps a **reference to the cursor node** and recomputes the
> cursor line from it. So changing *other* nodes preserves the cursor for free.
> But that reference is only reassigned when the cursor moves — remove the node
> the cursor is on and it dangles, pointing at a detached node whose stale line
> then decides where the cursor lands. The cursor is therefore moved off a node
> *before* that node is removed (to the next surviving sibling, else the
> previous, else the parent — never the top).

Tier 3 destroys node objects, so the cursor is restored afterwards **by key
path** — node ids and line numbers are meaningless across a rebuild. The
restore lands on a new node for the same object, which is not a new
selection, so it is not announced to the Detail pane again (see "A highlight
is not a jump" below). Expansion
state is carried across too; silently collapsing everything loses the user's
place just as surely as moving the cursor.

There is no insert-at-index in Textual's tree API. That single gap is why tier
3 exists at all.

One more constraint that bites repeatedly: a freshly created node has no line
number until the next refresh, so **any cursor move onto newly created nodes
must be deferred by one refresh** or it silently lands on the first row.
The deferred move holds a **key path**, not a node, because a rebuild can
land in between and destroy the node. A pending reveal also outranks that
rebuild's own cursor restore. Otherwise the restore puts the cursor back
where it was before the jump, and a search hit racing a cache update
leaves it on the database node instead.

### A highlight is not a jump

A tree highlight shows the object in the Detail pane and does nothing else.
Revealing (`ObjectTree.reveal`: expand down to a node, then move the cursor
there and centre it) is for a jump from outside the tree, which today means
a search hit or Enter on a row of a category list view. Otherwise the tree
cursor stays on the category, and nothing on screen names the object the
Detail pane shows.

The two travel as different messages: the tree's highlight posts
`ObjectSelected`, a list view's Enter posts `ObjectOpened`. Sharing one
message would make a show-only highlight take the list views' reveal with it.

Keeping highlight and reveal on separate paths is what prevents a loop, and
the trigger is a second Textual property:

> When the cursor's own node merely **changes line** -- something above it
> was expanded or added -- `Tree._build` reassigns `cursor_line` from the
> node, and `watch_cursor_line` compares against whichever node *now*
> occupies the old line. So Textual posts `NodeHighlighted` for the same
> node again, although the cursor never moved.

With the cursor on a table in a lower schema, a search jump to a table in a
collapsed schema above it expands that schema, which shifts the highlighted
table and re-highlights it. If a highlight also revealed, that would queue a
deferred cursor move back to the old table while the jump's own move was
still pending. Each move highlights its node, each highlight would queue
another move, and the two would alternate forever: the UI goes sluggish, the
tree scrolls by itself, and the detail pane is re-pointed on every move. No
Snowflake work is involved at all.

The tree also filters what it announces. It drops a highlight for a node the
cursor has already left, because that move posts a highlight of its own and
holding an arrow key would otherwise repaint the Detail pane for every
object passed. It drops Textual's repeat highlight of the node it announced
last, because each repeat would re-show the same table, resetting its sort and
cursor. `set_databases` forgets the last announcement: the screen clears the
Detail pane alongside it, so the next highlight has to be able to refill it.

---

## Detail views

The Detail pane hosts **exactly one view at a time**, chosen by whatever the
tree cursor is on.

Not a switcher holding every view mounted: with a growing set of object types
that means a dozen widget trees resident at all times, each needing a
coherent "nothing selected" state and each subscribing to updates it does not
care about. Mounting on demand keeps exactly one view alive.

**The swap is on object *type*, not on object.** Moving between two tables
re-points the existing view, so the widget, its columns and its scroll
position all survive — which is what makes arrowing down three hundred tables
cheap. A different type is a genuine change of shape, so that view is
replaced. Pointing the pane at what it already shows changes nothing at all.
Re-showing would repaint the view and reset its sort and cursor, and that
happens without the user moving: when the open table is picked again in
search, or when a search jump's own cursor move announces it.

**A view owns its whole body.** There is no separate context strip above the
Detail pane: selection-dependent content belongs to the view that shows it.

**A view declares which cache scopes it reads** (`scopes()`). That is how it
knows whether an update concerns it, and how its data joins the staleness
sweep.

### Four families, two registries

| Node under the cursor | View family | Found by |
| --- | --- | --- |
| an object leaf (`ORDERS`) | `ObjectView` | `view_for(type)` — registry, `@register_view` |
| a category node (`Tables (n)`) | `CategoryView` | `category_view_for(type)` — registry, `@register_category_view` |
| a schema node | `SchemaView` | constructed directly by `DetailPane.show_schema` |
| a database node | `DatabaseView` | constructed directly by `DetailPane.show_database` |

Both registries are module-level dicts populated by a decorator at import
time. No entry points, nothing to configure — adding an object type is one
new module plus one import. An object type with no registered view shows a
placeholder rather than crashing, so the tree can list types ahead of their
views existing.

A schema and a database go through no registry because neither has an
`ObjectType` and there is exactly one view for each — a lookup with a single
possible answer is a lookup worth not having. They share their whole
implementation (`_GrantsTagsView`) and differ only in which scopes they read,
since a database has no schema dimension.

### What decides a view's shape

Views vary in exactly three ways, and each is a consequence of what Snowflake
actually returns rather than a style choice:

- **A single `DataTable`** when there is one thing to show: `TableListView`,
  `ViewListView`, `StageListView`, `TaskListView`.
- **Tabs** when there are two genuinely different shapes for one node, with
  mnemonic switching (hidden from the footer, the pattern sibling project
  snakecam's `JobDetailsScreen` established): `PipeView` and `TaskView`
  (history + SQL), `TableView` (columns + data + grants), `ViewView`
  (columns + data + grants + SQL), `PipeListView` (activity + status),
  `SchemaView`/`DatabaseView` (grants + tags). Tabs are laid out in
  alphabetical order; which one opens first is set separately (`initial=`)
  and does not follow that order -- `PipeListView` opens on Status,
  `PipeView`/`TaskView` on History. A mnemonic means the same tab on every
  view: `d` is Data, so an object's defining statement is SQL (`s`) rather
  than Definition. Switching by mnemonic moves
  focus into the tab just shown: a mnemonic only fires while focus is inside
  the view, and Textual drops focus from a widget it hides instead of moving
  it, so the next mnemonic would otherwise go nowhere.
- **A detail panel under a table**, for what does not fit in a cell: the
  history views' error message for the highlighted row, a column's tags
  (`ColumnsView`), or a grantee's individual grants. It describes the
  highlighted *row*, never the object itself -- which is why a table's grants
  are a tab of their own and not a second panel under its column list: one
  would follow the cursor and the other never move. It shows itself when the
  row has something to say, rather than behind a toggle, because appearing
  is the signal: a load error showing up is the point.

A tab whose scope costs one round trip **per object**, and which is not the
tab a view opens on, is fetched only while it is shown. Today that is the
Table and View views' Grants and Data: the tree cursor passes over tables
and views far more often than anyone opens either tab, and each pass would
otherwise cost a `SHOW GRANTS` and a `SELECT` on top of the two queries an
object already makes. The `SELECT` would also wake a warehouse, and for a
view run the view's whole query (see "Bounding a result"). The scope is
listed in `scopes()` only while its tab is up -- the same conditional
`PipeView` applies to its history -- so the sweep and update routing ignore
it otherwise. Switching objects while on Grants or Data keeps the tab, and
so fetches each one's grants or rows, which is what is on screen.

The Pipes (n) list's Status tab follows the same conditional although it is
the tab the list opens on: its execution state costs Snowflake time per pipe
(see "A second source"), and a list left on Activity should not keep paying
for it. It also waits for the pipe list itself, which its pass names pipes
from.

Two of those decisions are worth their reasoning:

`PipeListView` is tabbed but `TaskListView` is not, even though a pipe and a
task look symmetrical. Status ("which of my pipes are broken?") needs *every*
pipe in the schema to be useful, so it lives on the list rather than on a
single pipe's view. A schema's tasks have only the one thing to show today,
so a single table is enough.

`ViewView` is a view's whole Table view plus one tab, because a view is
the one type that is genuinely **both** shapes the registry already had:
everything a table shows -- columns, data, grants -- (`ColumnsView` from
`table_view.py`, which holds all three tabs and is reused whole rather than
duplicated), *and* one defining SQL statement, like a pipe or task
(`show_views.text`, read through `Repository.object_detail` with no scope of
its own — the same way `show_pipes.definition` and `show_tasks.definition`
are). The SQL tab sorts after the others, so `ColumnsView.compose_extra_tabs`
is all a subclass needs.

A **stage** is the one type with a category view and no per-object view, and
that is deliberate rather than a gap: a `SHOW STAGES` row is the whole
picture, so `StageListView` is the only place a stage is shown at all.
Opening one still lands on the generic placeholder. The node stays expandable
anyway — nothing is saved by suppressing it, and `ObjectTree.reveal` would
have to special-case a stage hit to avoid force-expanding to a leaf otherwise
unreachable (Textual's `TreeNode.expand()` ignores `allow_expand`, which only
gates the user-facing toggle).

### Shared machinery

`ui/views/tables.py` holds what the views above have in common, and each
piece is there because duplicated versions drift:

- **`HoverTooltipTable`** — shows a cell's full-precision timestamp on
  hover, for the five list views whose columns are too narrow for one. It
  maps a **column header** to the field holding the untruncated value, not a
  *column index*, so reordering a view's columns cannot silently aim its
  tooltips at the wrong one.
- **`TabbedView`** — the mnemonic tab switch, in one place instead of five.
- **`_ScopedView.keeping_cursor`** — wraps a table's clear-and-repopulate
  so a repaint answering a cache update keeps the user's row. Without it,
  `DataTable.clear()` sends the cursor back to the first row each time an
  update lands, so a column's tags or a load's error would vanish while
  being read. The row is found again by key, else by position, so a table
  whose rows have no stable key (grants, tags) keeps at least its
  position. That is also why a pipe's load history is keyed by the load's
  merge key and not by its index. Showing a different object still starts
  at the top. The column list sorts synchronously inside the block, because
  tcoda's on-idle re-sort would otherwise move the rows after the cursor
  was placed.
- **`PanelledTable`** — a table plus the panel below it describing
  whichever row is highlighted, hidden when that row has nothing to say. Its
  consumers: `HistoryView` (below), for `PipeView`/`TaskView`'s load/run
  errors; `ColumnsView` (`table_view.py`), for a column's tags; and the
  grants table (`grants.py`). Only the describing function differs. It is a
  widget, not a view mixin: the Table view holds two (columns and grants),
  and a mixin has one panel per view and would answer every table's
  highlight with it.
- **`HistoryView`** — the two-source history table in a `PanelledTable`, and
  its capped/empty/errored note, shared by `PipeView` and `TaskView`.
- **`grants.py`** — the one Grants rendering, for the schema, database and
  table views alike (see "Column headers mirror Snowflake" for its shape).

The widget ids are shared too (`detail-list`, `detail-history`,
`detail-grants`, `detail-sql`, ...), and deliberately: exactly one
view is mounted at a time, so `#detail-history` means "whichever history
table is on screen", and an `app.tcss` rule written for one view's history
table, detail panel or SQL pane applies to every other view's too. The panel
is the exception, because one view can hold two: it is styled by the class
`detail-item`, and each panel's id is its table's plus `-item`
(`detail-columns-item`, `detail-grants-item`).

### Column headers mirror Snowflake

Headers follow the same rule as the cache: **Snowflake's own word, not a
nicer one.** Where two different Snowflake columns would collide in one
table, the header is qualified rather than renamed — `SHOW TASKS` and
`ACCOUNT_USAGE.TASK_HISTORY` both have a column called `state`, so the Tasks
(n) list shows "State" and "Last Run State", not "State" and "Status".
Likewise "Last Scheduled" is `SCHEDULED_TIME` and says so; a task scheduled
but never started still has one, which "Last Run" would have misrepresented.

The column list (tables, and a view's Columns tab) is the exception that
keeps short headers, because `INFORMATION_SCHEMA.COLUMNS`' names would widen
the table well past what its content needs. Instead, each header tooltips the
field it shows, in the cache's own upper-case vocabulary: "Ordinal" is
`ORDINAL_POSITION`, "Nullable" is `IS_NULLABLE`, "Default" is
`COLUMN_DEFAULT`, and so on, so a user can confirm the column is Snowflake's
and not a term nivaria made up. Type and Tags have no tooltip, because no
single Snowflake name would be true of them. Type is `DESC TABLE`'s full
rendering (see "`SHOW`, because it needs no warehouse"), which is more than
`DATA_TYPE` alone. `DATA_TYPE_ALIAS` was rejected: it is
the alias written in the DDL, such as `INT`, which SHOW COLUMNS does not
return and the cache does not hold. Tags is a count of the distinct tag
names in the `TAG_REFERENCES_ALL_COLUMNS` rows, not of the rows themselves,
since a multi-value tag has one row per value. A test holds every tooltip to a column the
cache actually has.

Type and Tags are computed, but only restate what the cache holds. No column
stands in for a Snowflake value nivaria has not read. The Pipes (n) Status
tab's **Execution State** is `SYSTEM$PIPE_STATUS`' `executionState` as
returned, and stays blank until it has been read, rather than being guessed
from `invalid_reason`: a paused pipe, or one whose target table or file
format was dropped, has no `invalid_reason` (see
[snowflake-behavior.md](snowflake-behavior.md#pipe-execution-state)).

**Grants are one row per grantee**, not one per `SHOW GRANTS` row: Grantee,
Granted To and Privileges, with OWNERSHIP first and `+` marking a privilege
held with grant option. The cache keeps every raw row regardless; this is
only how the tab shows them. The tab exists to answer "what may this role do
here?", and one row per privilege answers that only by scanning rows that
differ in a single cell. Grouping restates rather than invents -- every
value shown is Snowflake's, and each header tooltips the field it reads --
and it loses nothing: what cannot share one row (each privilege's
`granted_by`, `created_on`, `grant_option`) is listed in the panel under
the table for the highlighted grantee. `granted_on` and `name` are dropped
outright, since they are the same on every row of one object's grants. The
raw layout would serve an audit of who granted what and when better; the
panel covers that case one grantee at a time.

---

## Search

A modal (`SearchDialog`), opened by `/` at roughly 90 % of the screen. It is a
modal rather than an always-mounted widget in the top pane because search is
the primary way to navigate a database of thousands of objects, and it earns
the room only a modal can take without permanently stealing it from the tree
and detail pane. It is sized in `%`, not `fr`, because it wants a share of
whatever terminal it is in, not a fixed cell count.

Find-as-you-type over every cached object and schema across every selected
database, ranked by subsequence matching so `stgord` finds
`STG_CUSTOMER_ORDERS`. Substring search would not, which is the whole point;
sqlite's full-text matching cannot express it either, so ranking happens in
Python over three in-memory corpora — object names, column names (paired with
their owning object), and schema names. Column search is disabled (see
"Accepted design gaps"), but the corpus and its ranking logic below are
described as live, since the code is intact and only its one call site in
`SearchDialog.on_mount` is commented out.

A trigram/substring index (SQLite's FTS5) was considered as a way to push
matching into SQL instead. Rejected: it cannot express subsequence matching
either, and worse, cannot safely *pre-filter* for it — `STG_CUSTOMER_ORDERS`
shares zero substrings of length ≥2 with the query `stgord`, so a substring
prefilter would discard the exact match subsequence matching exists for,
silently, on every query that relies on it. The two are different matching
semantics, not one faster than the other for the same job.

### Loading: three corpora, independently

Each corpus loads as its own background task, kicked off in `on_mount` but
not awaited there — first paint is not delayed behind any of them. Object and
schema lists are cheap, but the column corpus is real work at scale (see
[measurements.md](measurements.md#search)). Fetching all three together would
mean the cheap ones wait on the slowest — splitting them means name and schema
matches are usable almost immediately, with column matches joining in once
their own load lands (while column search is disabled, only two of the three
loads fire).

Typing never waits on a load either: ranking proceeds against whatever
corpus already exists — empty, partial, or complete — and each load re-runs
the current query itself once it finishes, so the list corrects itself in
place rather than a keystroke ever blocking on it. **Enter is the one
exception**: choosing a result and closing the dialog is a commitment a
still-partial corpus should not get to make, so that path alone waits for
every load that is actually running first.

None of the three corpora carry live staleness tracking, for the same reason:
there is no widget left mounted for a cache update to mark them stale while
the dialog is open. `NivariaApp` routes `CacheUpdated` to `self.screen`,
which while the modal is open is the modal itself, not `MainScreen`, so a
background update would not reach a live corpus here any more than it
reaches the tree or detail pane while any other modal (the database picker,
say) is on top. Closing and reopening the dialog picks up whatever changed in
between.

### Matching and ranking

Object matching scores **both `SCHEMA.NAME` and the bare name**, keeping
whichever is better, with a small preference for the name. This is not
gold-plating: Snowflake schemas repeat themselves in their objects' names —
a `STAGING` schema full of `STG_` tables — and greedy left-to-right matching
otherwise spends the query's leading characters inside the schema, leaving a
poor alignment in the name. Without this, `stgord` ranks `STAGING.LOAD_ORDERS`
*above* `STAGING.STG_CUSTOMER_ORDERS`.

The displayed text also carries a `DATABASE.` prefix ahead of `SCHEMA.NAME`,
since several databases can be selected at once — but that prefix is
added purely at render time (`SearchDialog._option_text`) and deliberately
excluded from ranking itself. Extending the schema/name qualification to a
third, database level would spend the query's leading characters on the
database too, for no benefit: there is no database-repeats-inside-object-name
ambiguity the way `STAGING`/`STG_` justifies it for schema. Don't assume the
database participates in match scoring the way the schema does — it doesn't.
It can still be *matched*, by a filter word (below), just never scored.

**Multi-word queries: the first word ranks, the rest filter.** Everything
above describes the query's first word; it is matched, scored and ordered
exactly as a one-word query. Each further word, after a space, only
narrows: a row survives if every filter word fuzzy-matches inside *one* of
its parts — database, schema or name — so `MY_TEST_TABLE my_test_schema
test_db` walks a name repeated across databases down to one row. Filters
never touch the score, so adding a word can only drop rows, never reorder the
list or bring one back. An fzf-style reading, where every word scores
equally in any order, was rejected for exactly that: a lone word must keep
the database out (otherwise `prod` lists every object in `PROD_DB`), so a
second word would change what the first may match and the list could grow
or reshuffle while the user narrows it. Matching a filter one part at a time
rather than across `DB.SCHEMA.NAME` is what makes it narrow at all — across
the dots, a subsequence like `test_db` can borrow letters from all three
parts and matches nearly everything. Filters are applied to the corpora
*before* ranking, because ranking stops at `MAX_RESULTS`: filtering its
output would only see that top slice, and a name repeated across enough
databases pushes the wanted row past it. Each query is ranked from scratch,
not by narrowing the previous list, which would break on backspace, a
mid-query edit or a paste. Whatever a filter word matched is highlighted,
including in the otherwise dim database prefix, so a row shows why it
stayed.

Column and schema names are matched against the bare name only — there is no
schema-repeats-in-object-name ambiguity to resolve for either, so the simpler
matcher is enough. A table matched by both its own name and one of its
columns keeps whichever scored higher; a schema hit never competes with an
object/column hit for the same slot, since a schema is its own kind of thing
(see `SchemaRef` below), not a rival reading of the same one.

Below `MIN_COLUMN_QUERY_LENGTH` characters (a plain module constant in
`search.py`, not a Setting — a tuning knob for one machine's cache size, not
a per-user preference), ranking skips the column corpus entirely; object and
schema names are still matched at any length. A short, generic fragment
scores against every cached column for little relevance benefit and, with
a large column cache, real per-keystroke latency. Even so, this only skips
queries shorter than the constant — a longer fragment still costs whatever
ranking the full column corpus costs; the constant does not bound that.

Ranking runs in a thread (`asyncio.to_thread`), not on the event loop, and the
input is debounced on top. Object corpora alone would be comfortable on the
event loop, but the column corpus routinely outnumbers the object corpus
10-20x (see [measurements.md](measurements.md#search)). The matcher is a plain
function precisely so this stays an easy change.

### Choosing a hit

Choosing an object or column hit **reveals it in the tree** (`ObjectTree.
reveal`) as well as opening it in the detail pane, so search leaves you
somewhere you can keep browsing from. The object is scrolled to the middle
of the tree rather than the least distance into view, which would pin a hit
further down to the last visible row. A **schema** hit is different —
`SchemaRef` carries only `database`/`name`, deliberately not an `ObjectRef`
with some placeholder type, since a schema has no `ObjectType` and no mirror
row of its own to show. Choosing one instead **reveals and expands it** in
the tree (`ObjectTree.reveal_schema`), which moves the cursor onto the
schema node itself — and since a schema node announces itself on
highlight (`SchemaSelected`, see "Detail views"), that
also opens its Grants/Tags view, not just the tree navigation alone.

---

## Status footer

The right corner carries four indicators: Role, Warehouse, Countdown and Pool
gauge (what each shows is in the
[manual](MANUAL.md#status-footer)). Textual's command-palette binding is
suppressed so the corner is available; the footer is otherwise Textual's, so
key bindings keep working without being re-implemented.

Role and Warehouse are read straight off the pool's connect params each tick.
Leaving either field blank in the connect dialog does not mean "none" is shown
forever: Snowflake always activates *some* role for a session (the user's
default, or `PUBLIC`), and the driver already learns what it resolved from the
login response (`sessionInfo.roleName` / `.warehouseName`, exposed as
`connection.role` / `.warehouse`) — no extra query needed. `connect_all`
adopts whichever of the two was left blank from that
(`ConnectionPool._adopt_resolved_session`), so the footer shows the real
session state rather than an unfilled form field. Warehouse can genuinely
read `none` even after this — a session can have no active warehouse at all
— but role never legitimately does.

The login response has no organization or account name, which Snowsight links
(`w`) need. `connect_all` asks for them once, on the first connection
(`ConnectionPool._read_account_names`, context functions only, so no
warehouse), and keeps them on the pool; a link is then an attribute read and
never waits on Snowflake. A failure leaves them unset and does not fail the
connect: the link is a convenience, the session is not. They only build the
public address: a PrivateLink account's Snowsight host depends on its network's
DNS, which the client cannot see, so it comes from the per-account Snowsight
URL setting, which wins whenever it is set.

The Role and Warehouse pickers (`PickerDialog`, single-select; the database
picker is the multi-select sibling, `MultiPickerDialog`) are backed by their
own cache scopes (`SHOW WAREHOUSES`, `SHOW ROLES` — small, flat, unpaged
lists, unlike the object mirrors), requested eagerly alongside the database
list rather than on the picker's first open — on mount, and again after a role
switch or a reconnect, since either points the app at a different cache file.
Fetching lazily on the picker's first open would make that open just request
the list and close again, since there would be nothing cached yet to show.
Unlike the database picker, which only ever changes what a query is built to
ask for, picking a warehouse or role actually rewrites the session: see
"Switching warehouse or role mid-session" below.

The Countdown is one number for the whole screen, not a per-scope freshness
readout: the time until the first thing on screen goes stale (see "The
staleness sweep"). It has a fixed width, right-aligned,
no leading zeroes, so the digits do not shift as it counts down.

The gauge is polled a few times a second rather than pushed from the pool. Pool
usage changes on worker threads, so pushing would require marshalling on every
acquire and release; polling a lock-guarded integer is simpler, has no thread
hazards, and is still fast enough to register a single round trip. The
warehouse/role labels ride the same poll for the same reason.

### Switching warehouse or role mid-session

Unlike the database picker, `SHOW WAREHOUSES` and `SHOW ROLES` carry no
per-query qualifier — role and warehouse are session state, and the pool holds
several physical sessions. Picking one therefore runs `USE ROLE` / `USE
WAREHOUSE` on **every** pooled connection (`ConnectionPool.run_on_all`), not
just whichever one a checkout would have handed out next, or later results
would depend on which connection happened to serve a given query.

That statement must never run concurrently with a worker's query on the same
connection — the driver does not support two cursors in flight on one
connection object — so `run_on_all` drains every connection out of the free
queue first (blocking on whatever is still mid-query, the same as an ordinary
checkout) and holds a pool-wide lock that `acquire()` also waits on, so nothing
can be checked out mid-rewrite. This is not transactional across connections:
if the statement fails on one after already succeeding on another (a network
blip, say), the pool is left with a genuine split and the error surfaces to the
user rather than being papered over — full atomicity would need Snowflake's
cooperation across independent sessions, which does not exist. The common
failure mode, an invalid role or warehouse name, cannot happen here since the
picker only ever offers names `SHOW ROLES` / `SHOW WAREHOUSES` actually
returned.

#### What a role change does to the cache

A role switch does not touch the cache at all in the sense above — no row is
deleted or invalidated. Instead the *file* changes: `MainScreen` is repointed
at a different `Repository`, one sqlite file per (account, role). See "One
cache file per identity, not a role dimension on Scope" for why, and for the
fetch-service sequencing a same-pool role switch requires. Warehouse switches
skip all of this: `SHOW` is metadata-layer and consults no warehouse, so
nothing about the cache needs to change when it does.

---

## The SQL log

The `F1` log panel is `tcoda`'s `LogViewer` (see "Shared widgets"), mounted in
`MainScreen.compose` with its defaults, so nivaria wires nothing for it: the
widget installs its own key binding and its own screen layer, owns a rolling
in-memory buffer that exists whether or not the panel is mounted, and offers
level and source filters of its own.

What nivaria contributes is the content. **`gateway/sql_log.py` logs every
statement before it is sent**, not after, and that ordering is the point:
`cursor.query` — the driver's public "what did I run" attribute — is only
populated once `execute()` has already returned, which answers nothing about
a connection that is currently hung. `Cursor._preprocess_pyformat_query` is
what the connector itself calls, synchronously, just before the round trip,
so reusing it means the logged text is guaranteed to be what actually goes
over the wire with no pyformat substitution re-implemented here.

It deliberately does *not* route through `Connection._format_query_for_log`,
which truncates to 80 characters by default — sized for the connector's own
terse debug stream, not for "show me the whole statement". That function's
other job, masking credentials, is not reproduced either: it exists for
statements that can embed cloud storage secrets (`COPY`, `CREATE STAGE
CREDENTIALS=(...)`, `PUT`/`GET`), and `queries.py` — the only place any SQL
text is built at all — never constructs one; every statement is a `SHOW`, an
`INFORMATION_SCHEMA`/`ACCOUNT_USAGE` `SELECT`, a Data tab's `SELECT * ...
LIMIT`, or a bare `USE ROLE`/`USE WAREHOUSE`. Reaching for that masking would also mean importing
`snowflake.connector` from a module `gateway.py` and `pool.py` import
unconditionally — exactly what the fake-gateway seam exists to keep out of
every test.

Logging is gated on `_log.isEnabledFor(DEBUG)`, so with the panel's filter
anywhere above Debug the whole path costs one level check per statement.

---

## Settings

Settings are declared once and self-register, so a settings UI can enumerate
them with no per-setting wiring. Only values that differ from their default are
persisted -- a setting nobody has touched stays absent from the file, so a
future release can change its default without a stale override shadowing it.
Values are read **at the point of use**, never cached, so a change takes effect
without a restart. The full list is in the
[manual](MANUAL.md#settings).

All of that machinery -- `Setting`, the registry, `ConfigStore`, the dialog --
is `tcoda`'s (see "Shared widgets" below). `nivaria/settings.py` is only the
declarations plus the one `configure("nivaria")` call that tells the shared
store which app's config file to use; the store resolves that path lazily, on
first read, so importing the module touches no disk.

The bounding settings (`HISTORY_MAX_ROWS`, `DATA_PREVIEW_ROWS`,
`DATA_PREVIEW_TIMEOUT`, `MAX_PAGES_PER_PASS`, `ROW_CEILING`,
`PRIME_CHUNK_THRESHOLD`) exist purely so the measures in "Bounding a result"
are adjustable rather than baked in. Their defaults are starting points, not
measured optima — an account whose shape makes one of them wrong should be
able to say so without a code change.

`PAGE_SIZE` is the one setting **not** read at the point of use: it is
snapshotted into `FetchService` at construction, so a change only takes effect
on the next `restart_fetch` (a reconnect or a role switch). Not deliberate,
just unfixed — and worth knowing when lowering it to reproduce a paging
problem.

A value that fails to parse in the dialog (`ValueError`) blocks OK/Apply with
an inline warning rather than silently discarding it or falling back to the
default -- that fallback exists for a hand-edited settings.json, not for a
mistake made in the dialog itself. Cancel confirms first if anything was typed
that differs from what was last saved -- the same "unsaved changes" guard
`ConnectScreen` uses.

`tcoda`'s dialog also supports per-instance settings and callback-only action
rows, which nivaria declares none of.

---

## Shared widgets

The menu bar, the settings system, the log panel (`LogViewer`, see "The SQL
log"), the dialog chrome underneath the dialogs (`Dialog`, `ConfirmDialog`)
and the terminal's window title (`TerminalTitleMixin` on `NivariaApp`) live
in **`tcoda`** ("Textual coda") because they are shared between different
projects.

It is an ordinary runtime dependency from PyPI, bounded to one minor series in
`pyproject.toml`; see
[DEVELOPMENT.md](DEVELOPMENT.md#testing-against-unreleased-tcoda) for working
against unreleased changes.

Things worth knowing about it from inside nivaria:

- **`tcoda` ships its TCSS inline**, as `DEFAULT_CSS` on each widget, rather
  than in a stylesheet -- a library cannot rely on a consumer's `CSS_PATH`.
  So the menu-bar and settings-dialog rules are not in `app.tcss`.
  `app.tcss` outranks `DEFAULT_CSS` regardless of specificity, so anything
  tcoda does style can still be overridden here.
- **`MenuBar` installs its own `F9` binding** on whichever screen it is
  mounted on, which is why `MainScreen.BINDINGS` carries none. Textual only
  consults the focus chain plus the screen and the app when resolving a key,
  so a bar that is never focused cannot own the binding itself.
- **nivaria only declares what is in the menu bar**, inline in
  `MainScreen.compose`: the staged bar-only/dropdown flow, mnemonics and
  separators are `MenuBar`'s. **About ...** is a `tcoda.widgets.AboutMenuItem`
  reading `ui/about.txt` through a real filesystem path rather than just an
  `importlib.resources` handle, and **Settings ...** a
  `tcoda.widgets.SettingsMenuItem`, which opens the dialog itself, so nivaria
  wires nothing for it. Choosing any other entry posts `MenuItem.Pressed`,
  handled with `@on(MenuItem.Pressed, "#database")` and the like on
  `MainScreen`, which hands off to the same `@work` wrapper the matching key
  binding uses -- a plain message handler has no worker context, and
  `push_screen_wait` needs one.
- **A `shortcut` label advertises a key binding**, independent of `mnemonic`
  (which drives in-menu letter selection). `Ctrl+O`/`Ctrl+D`/`Ctrl+R`/`Ctrl+W`
  are live key bindings hidden from the footer (`show=False`): the menu's
  `shortcut` label is where they are advertised, keeping the footer down to
  the bindings someone would reach for without having opened the menu first.
- **A `Menu`/`MenuItem` id is also a CSS id.** Textual only requires it to be
  unique among one menu's own children, but any `app.tcss` rule of the same
  name styles it too -- and for a widget inside a one-row bar that does not
  show up as a wrong colour, it shows up as a garbled row of scrollbar
  blocks. The Connection menu is `#connection-menu` rather than `#connection`
  for exactly this reason: `#connect` (note the different word) is the
  connect dialog's container, and `#connection` sits close enough to invite
  the same mistake later. `test_menubar.py` asserts the title's *geometry*,
  not just its text, so a future clash fails a test instead of only looking
  wrong.

The tree/Detail split's drag-to-resize border uses the same mechanism as
sibling project snakecam's `ResizableTree`: dragging `ObjectTree`'s right
border overwrites its initial `fr` width with a fixed cell width, and the
Detail pane's own `fr` absorbs whatever is left.

---

## Accepted design gaps

- The Pipes (n) Activity tab, the Tasks (n) list and a pipe without a known
  target read only `ACCOUNT_USAGE`, so they lag by however long Snowflake
  takes to publish, up to ~2 h for copy history. The lag line says so; no
  query can remove it.
- A row that `ACCOUNT_USAGE` publishes later than the lag margin is never
  asked for by a window pass. It stays missing until a full pass:
  `MainScreen.full_refresh` exists for that but has no key yet. For a list
  that holds each pipe's or task's latest row, it only matters if no later
  load or run follows.
- Rows of a dropped pipe or task stay in a log scope's cache until they age
  out of the fetch window, since a window pass sweeps nothing it did not
  return. The lists are driven by `SHOW`, so they do not show them, but a
  pipe re-created under the same name resolves its target from the old row
  until its first load.
- The tree/Detail drag-resize does not persist across sessions, and does not
  re-clamp an already-narrow pane if the terminal is then resized smaller.
- A single stage's own leaf node has no Object view. Deliberate, not an open
  gap: see "What decides a view's shape".
- **Performance at a large multi-database selection is an open question.**
  Several databases can be browsed and searched at once, and nothing bounds
  total object counts across them; it simply has not been hit at the scales
  exercised so far.
- Databases aren't themselves a *searchable hit* the way a schema is:
  selecting one is a picker action, not a search result.
- Column search is implemented end to end at the repository layer
  (`Repository.search_column_corpus`) but its one call site in
  `SearchDialog.on_mount` is commented out — cross-database column search was
  judged not ready. Restoring that call alone would find only the columns of
  tables someone has opened: nothing fills a database's columns in bulk.
- The Data tab shows what the driver returns, and the driver loses a little
  on the way: a `DECFLOAT` is rounded to 28 significant digits, `TIME` and
  `TIMESTAMP` values are cut to microseconds, and a `TIMESTAMP_LTZ` comes in
  the session's `TIMEZONE` rather than the machine's. Its other cells keep
  their meaning (see
  [snowflake-behavior.md](snowflake-behavior.md#result-types)).
- `CONNECT_TIMEOUT_SECONDS` (`gateway/pool.py`) bounds a denied or abandoned
  approval for OAuth and password+MFA, but not for browser SSO
  (`externalbrowser`): the installed driver waits on a socket with no timeout
  that nothing can reach (see
  [snowflake-behavior.md](snowflake-behavior.md#externalbrowser-login)). An
  abandoned browser tab under that authenticator can still leave the
  "Connecting ..." dialog counting down to 0 and sitting there indefinitely. A
  client-side watchdog was considered and rejected for now: the driver gives
  no way to reclaim the blocked thread or its open listening socket, so a
  watchdog could only abandon the dialog, not the resource underneath it.
- Adding a scope touches ten places (the checklist is in
  [DEVELOPMENT.md](DEVELOPMENT.md#adding-a-scope)). A table-driven scope
  registry would collapse most of it, but it is a large change with no bug
  behind it, so the checklist stands until it starts hurting.
