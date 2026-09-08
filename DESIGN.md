# snowcone — design

A terminal UI for browsing and searching Snowflake objects quickly.

This document describes how snowcone works and why. It is the context a new
working session needs before changing anything. It deliberately avoids file
layout and code: read the modules for those.

---

## The one rule

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

---

## Screen anatomy

Use these names when discussing changes.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            Menu bar (F9)                                 │
├────────────────────┬─────────────────────────────────────────────────────┤
│ ┌─────────────────┐│                                                     │
│ │  MYDB (12)      ││                                                     │
│ │  OTHERDB (5)    ││              Detail pane                            │
│ │   Object tree   ││        (hosts exactly one Object view;              │
│ │                 ││   drag this border to resize either pane)           │
│ │                 ││                                                     │
│ └─────────────────┘│                                                     │
├────────────────────┴─────────────────────────────────────────────────────┤
│ ^d Databases  / Search  r Refresh  F10 Quit  Role  Warehouse  Countdown  (2/5)│
└──────────────────────────── Status footer ───────────────────────────────┘
```

`/` opens the **Search modal** over this screen, at roughly 90 % of the
terminal:

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ┌────────────────────────────────────────────────────────────────────┐   │
│ │ search objects                                                      │   │
│ │ ......................................................................│
│ │  MYDB.STAGING.STG_CUSTOMER_ORDERS                            table  │   │
│ │  MYDB.SALES.ORDERS                                              view │   │
│ │  OTHERDB.RAW.LOAD_ORDERS                                       pipe │   │
│ │  ...                                                                │   │
│ └────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

| Name | What it is |
|---|---|
| **Menu bar** | Midnight-Commander style: hidden except while in use, opened by `F9` or by clicking a title. The bar itself -- the staged bar-only/dropdown flow, mnemonics, separators, the `F9` binding -- is `tcw`'s `MenuBar` (see "Shared widgets" below); snowcone only declares what is in it, inline in `MainScreen.compose`. Currently one menu, **Connect** (`c`), with **Open...** (`o`) to reopen the connect dialog and swap in whatever pool it returns, then a separator, **Databases ...** (`d`, `Ctrl+D` shown right-aligned) to open a **multi-select** fuzzy picker (`MultiPickerScreen`, see "The database picker" below) over cached databases, **Warehouse ...** (`w`, `Ctrl+W`) and **Role ...** (`r`, `Ctrl+R`) to open the still-single-select fuzzy picker (`PickerScreen`) over cached warehouses/roles, applying the choice session-wide (see below), and **Pre-warm cache** (`p`) to fill the column cache of whatever database the cursor is currently under, in the background -- see "Column cache priming"; then another separator, **Settings ...** (`s`) -- a `tcw.widgets.SettingsMenuItem`, which opens the dialog itself, so snowcone wires nothing for it; then another separator and **Quit** (`q`, `F10` shown right-aligned), which runs `app.quit`. Choosing an entry posts `MenuItem.Pressed`, handled with `@on(MenuItem.Pressed, "#database")` on `MainScreen`, which hands off to the same `@work` wrapper the matching key binding uses -- a plain message handler has no worker context, and `push_screen_wait` needs one. A `shortcut` label advertises a key binding that already does the same thing, independent of `mnemonic` (which drives in-menu letter selection). `Ctrl+D`/`Ctrl+R`/`Ctrl+W` and `F10` all stay live as key bindings but are hidden from the footer (`show=False`) -- the menu's `shortcut` label is where they're advertised, keeping the footer down to the bindings someone would reach for without having opened the menu first. |
| **Object tree** | The browser. One top-level node per selected database, each showing its own schema count directly (`MYDB (12)`) and expanding straight into schemas -- → schema → `Tables (n)` / `Views (n)` / … → object. No border title: several databases can be selected at once, so there is no longer one canonical database to name there. `ctrl+d` opens the **Databases picker**, a multi-select fuzzy modal over cached databases -- every selection gets its own top-level node, and search (below) runs across all of them. |
| **Search modal** | Find-as-you-type over every cached object and schema across every selected database (column search is currently disabled, see "Known gaps"), opened by `/` at roughly 90 % of the screen -- search is the primary way to navigate a large database, so it gets a screenful of ranked matches rather than a sliver of the top pane. `enter` opens the highlighted hit and closes the modal; `escape` closes it without choosing. Choosing a hit reveals it in the tree as well as opening it, so it leaves you somewhere you can keep browsing from. |
| **Detail pane** | Hosts exactly one **Object view** at a time, chosen by object kind. Its shared right border with the Object tree is mouse-draggable, resizing both panes live. |
| **Object view** | The per-kind detail UI (e.g. Table view, Pipe view). Owns its whole detail body. |
| **Status footer** | Key bindings on the left; **Role**/**Warehouse** labels, **Countdown** and **Pool gauge** in the right corner. Textual's command-palette key is suppressed so the corner is ours. |

The tree/Detail split starts at 25 % / 75 % of the width, in `fr` units, and
is now genuinely resizable: dragging `ObjectTree`'s right border (the same
mechanism as sibling project snakecam's `ResizableTree`) overwrites that `fr`
value with a fixed cell width, and the Detail pane's own `fr` absorbs
whatever is left. Neither pane can be dragged narrower than a small minimum.
The drag is not persisted across sessions, and a terminal resize does not
re-clamp an already-narrow pane -- both accepted gaps, see "Known gaps".
The search modal alone is sized in `%`, not `fr`, on purpose: it wants a
share of whatever terminal it is in, not a fixed cell count.

---

## The cache

### Shape: mirror Snowflake, don't reinterpret it

The bottom layer is **one table per `SHOW` command, with the result columns
named exactly as Snowflake names them** — `created_on`, `database_name`,
`cluster_by`, `invalid_reason` and so on. Column sets were verified against a
live account (Snowflake 10.31.103) rather than taken from documentation.

Only a useful subset of each result is mirrored; columns Snowflake returns that
we do not mirror are dropped silently on ingest, so Snowflake adding a column
is a non-event.

Kinds are **not** collapsed into one generic table. `SHOW PIPES` has
`definition` and `notification_channel`; `SHOW TABLES` has `rows`, `bytes`,
`cluster_by`. A lowest-common-denominator table would lose those and invent
vocabulary that does not exist in Snowflake. The repetition is the cheaper
price.

Two derived layers sit on top, and only these two:

- a **uniform object view** used by search, flattening every kind to
  `(database, schema, name, kind)`;
- the **freshness ledger**, below.

Bookkeeping columns are underscore-prefixed so they can never collide with a
Snowflake column name.

### Scopes: the unit of everything

A **scope** is one separately-refreshable set of rows — the databases list, one
database's schemas, one schema's tables, one table's columns. A scope is a
single hashable value, which is what lets deduplication, update routing and
staleness all be dictionary lookups.

Scopes are **independent of one another**. It is tempting to think a schema's
tables must wait for that database's schema list to finish refreshing, but each
query is self-contained, so they simply run concurrently. There is no
dependency graph, no ordering constraint and no deadlock surface. If a schema
really was dropped, its tables fetch fails, that failure is recorded, and the
schema refresh removes the node on its own next pass.

### One cache file per identity, not a role dimension on Scope

A scope is keyed by database/schema/object/table — never by role. That is
deliberate, not an oversight: `database` can be part of a scope key because it
is explicit in the statement text (`SHOW SCHEMAS IN DATABASE X`), but role has
no such syntax. It is pure session state, invisible in the SQL, so "which
role was active" cannot be recovered from a scope alone — it would have to be
stamped onto every row at fetch time from whatever the connection's role
happened to be *right then*, which is a much bigger seam to open (every scope
constructor, every mirror table's key, a schema migration) for a problem that
has a far cheaper fix.

That fix: `SHOW` output is filtered by role, so the whole cache — not one
table, not one scope kind, all of it — is only ever valid for the role that
was active while it was built. So instead of tagging rows, snowcone keeps a
**separate sqlite file per (account, role)** (`paths.cache_file`), and treats
switching roles as switching *which file* `Repository` points at, the same way
`open_connect` already swaps in a whole new `ConnectionPool`. Within one file,
everything above (scopes, the pass protocol, staleness) is exactly as
described and needs no changes, because it was already correct for a single,
fixed identity — the one thing that used to not hold across a role switch.

This is also why it does not cost a wipe: switching back to a role visited
earlier in the session reopens that role's own file, warm, rather than
refetching everything. An admin who alternates between two roles all day
pays one refetch per role the *first* time they use it that session, not
every time they switch. Account is part of the key too, not just role, so
two different accounts that happen to use a same-named role (a generic
`SYSADMIN` exists in plenty of accounts) cannot share a file either. Warehouse
is deliberately excluded from the key: `SHOW` needs no warehouse (see below),
so nothing it returns can vary with which one is active.

Because the identity is not known until a connection exists, the cache is not
opened at app startup — `SnowconeApp.on_mount` no longer creates a `Repository`
before the connect dialog even runs. It is opened once a pool exists
(`SnowconeApp.switch_repo`), and re-opened by the same call after a role
switch or a reconnect. `MainScreen.set_repo` (and the matching method on
`ObjectTree`, `DetailPane`, `SearchBar`) is what repoints a widget at a
different `Repository` object entirely; it resets browsing state exactly like
a fresh connect would, since whatever was on screen was read out of a file
that no longer applies.

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
row count and last error. This is what makes a refresh *surgical* rather than
"fetch everything again".

Refreshing a scope is a **pass**:

1. begin — claim the scope and bump its generation;
2. write — upsert rows, each stamped with that generation (repeatable);
3. finish — delete rows in this scope *older* than this generation, then mark
   it complete.

Deletion only at the end is the point. Rows are never briefly absent, so the UI
never flashes an empty list on the way to a full one, and a chunked refresh is
safe to display while still running. An object that really was dropped
disappears exactly one pass later.

A write carrying a stale generation is rejected. Thread workers cannot be
interrupted, so a fetch that was already in flight when the user navigated away
is fenced on arrival instead.

A **failed** pass does not sweep. Showing yesterday's tables beats showing an
empty schema because the warehouse was briefly unreachable.

The cache is an optimisation, never a dependency: if it cannot be opened the
app still runs, it just has nothing cached. It uses WAL journalling so a
writing worker never blocks the reading UI, and opens a connection per call
rather than holding one, so the same code is usable from the event loop and
from worker threads.

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
each field — only the source changed. A type that cannot be parsed degrades to
"unknown"; a column snowcone cannot describe is still a column worth listing.

### A second source: one new mirror table per SELECT, never a retrofit

`SHOW` remains the strong default above: it needs no warehouse and covers
almost everything the tree and detail pane show. But some facts genuinely do
not exist in any `SHOW` output — a table's last-DDL/last-altered timestamp is
only in `INFORMATION_SCHEMA.TABLES`, an ordinary `SELECT` that needs a
running warehouse. Rather than treat that as a reason to avoid the feature,
or as a reason to make the whole detail pane depend on a warehouse, such a
feature gets its own scope, needing its own warehouse, degrading on its own:
a failed or missing pass for that one scope renders blank, while everything
`SHOW`-sourced in the same view keeps showing yesterday's data. This is
free — a SELECT-backed scope is an ordinary `Scope`/`TableSpec`, so
`begin_pass`/`write_page`/`finish_pass`/`fail_pass`, the staleness sweep, and
"a failed pass does not sweep" all apply unmodified. It also gets no special
treatment from the sweep itself: a scope currently on screen is refreshed on
the same TTL as everything else, warehouse or not — the sweep already only
ever touches what is on screen, so there is nothing extra to opt in or out of.

The shape rule is the mirror convention above, applied literally: **never
retrofit a column onto an existing mirror table** — `schema.sql` is all
`CREATE TABLE IF NOT EXISTS`, with no migration system, so there is no safe
path to `ALTER` one. Each new SELECT-backed source instead gets a brand-new
mirror table, named and shaped like the INFORMATION_SCHEMA view it reads
(upper-case columns) — the same exception `information_schema_columns`
already established for `SHOW COLUMNS`, just literally true this time
instead of a SHOW result mapped onto that vocabulary.

`information_schema_tables` (`LAST_DDL`, `LAST_ALTERED` on the "Tables (n)"
list) is the first instance of this. `account_usage_copy_history` (Rows,
Size, Loaded, Status, Target Table, Filename on the "Pipes (n)" list) is the
second: each pipe's *latest* load in a schema, read from `SNOWFLAKE.
ACCOUNT_USAGE.COPY_HISTORY` rather than `INFORMATION_SCHEMA` — the latter's
`COPY_HISTORY` table function requires a `TABLE_NAME` argument with no
schema-wide form, which would mean one round trip per distinct target table
(worst case one per pipe) every time the list opens, against the
round-trips-dominate principle below. `ACCOUNT_USAGE.COPY_HISTORY` is an
ordinary view filterable by `PIPE_CATALOG_NAME`/`PIPE_SCHEMA_NAME`, so one
query covers a whole schema's pipes at once, the same shape `table_stats`
uses for tables — bound by the "pipe history fetch window" setting (default
90 days) rather than a fixed lookback, and reduced to one row per pipe with
a `QUALIFY ROW_NUMBER() OVER (PARTITION BY PIPE_NAME ORDER BY LAST_LOAD_TIME
DESC) = 1`, since the list only ever wants each pipe's most recent load and
so does `Repository.pipe_target_table`'s resolution (below). Collapsing this
in SQL, not just in Python the way the list already did, is the fix for a
real incident: on an account with ~400 pipes and frequent ingestion, the
unreduced query returned unboundedly many historical rows — one per load,
over 90 days, across every pipe in the schema — and the app hung for
minutes fetching and materializing all of them just to throw away everything
but the newest per pipe a moment later. The trade-off that remains: it needs
`IMPORTED PRIVILEGES` on the `SNOWFLAKE` database (or `ACCOUNTADMIN`) and can
lag real activity by up to ~2 hours — both are just another way this one
scope degrades blank, the same as a missing warehouse does for `table_stats`,
so nothing new had to be built to accommodate them.

A single pipe's own "Load history" needs more than the latest row, so it
does *not* share this scope — it has its own, `pipe_activity_history`,
querying the same view with the same fetch-window bound but filtered to one
`PIPE_NAME` instead of `QUALIFY`-reduced, and — like `information_schema_
copy_history` below — fetched lazily, only once that pipe's detail view is
open, never eagerly for every pipe in the schema. Unlike that
`INFORMATION_SCHEMA` sibling, this needs no target-table resolution first:
`ACCOUNT_USAGE.COPY_HISTORY` already carries `PIPE_NAME` as an ordinary
column. It also gets its own mirror table (`account_usage_copy_history_
detail`) rather than reusing `account_usage_copy_history` under a different
`_scope` value — that table's primary key predates having two different
scopes write into it and does not include `_scope`, so a row visible to both
the schema-wide latest-per-pipe scope and a per-pipe backfill scope would let
whichever pass wrote it last silently steal its `_scope` tag from the other.

`FIRST_ERROR_MESSAGE`/`FIRST_ERROR_LINE_NUMBER` were added later, directly
onto both `account_usage_copy_history` and `information_schema_copy_history`
below — the pipe detail view's Load History tab shows them for whichever
row is currently highlighted. This is a deliberate exception to "never
retrofit a column onto an existing mirror table" (above), not a
counterexample to it: snowcone is pre-1.0 with no migration system and no
backward-compatibility policy, so the usual reason to avoid a retrofit --
an old cache file's table keeps its old shape and a write to the new column
then crashes (`schema.sql`'s `CREATE TABLE IF NOT EXISTS` never alters an
existing table) -- is resolved the simpler way here: `schema.sql` is edited
in place, and anyone with a pre-existing cache file deletes it after
upgrading. A new mirror table (as the rule above would otherwise call for)
stays the right move whenever a straight cache wipe is not an acceptable
answer.

`information_schema_copy_history` is the third instance, and the first to
pair with another SELECT-backed scope rather than stand alone: a single
pipe's "Load history" merges it with `account_usage_copy_history_detail`
above (that pipe's own backfill, not the schema-wide `account_usage_copy_
history` the list uses).
`INFORMATION_SCHEMA.COPY_HISTORY` gives near-real-time data but only for the
last 14 days (Snowflake's own retention limit for that function, not a
setting), and awkwardly requires a `TABLE_NAME` argument rather than a
`PIPE_NAME` one — Snowflake exposes no way to ask it for "this pipe's"
history directly. Rather than parse the pipe's target table out of its COPY
INTO definition text, `Repository.pipe_target_table` reads it straight off
whatever `account_usage_copy_history` already has cached for that pipe:
Snowflake's own resolution, already being fetched for the "Pipes (n)" list
anyway, not a guess. That is an opportunistic read of a sibling scope's
cache, the same pattern `_fan_out_columns_by_schema` already uses for
`SCHEMAS` — not a hard dependency between scopes.

A pipe that has never loaded anything has no cached activity row to resolve
a target from, and that is a normal, *permanent* state for plenty of real
pipes — not a transient failure to retry. Treating it as one was the first
version's mistake: it queued `PIPE_HISTORY` anyway, `fetch_page` raised for
lack of a target, and the fetch layer dutifully reported that as an error on
every fetch, forever, for a pipe that will never resolve one. The fix lives
above the fetch layer entirely: `PipeView.scopes()` only lists
`pipe_history_scope` once `pipe_target_table` actually resolves something,
and `update_content` only calls `ensure()` on it under the same condition.
An unresolvable pipe is therefore never queued at all — by the interactive
path or by the background sweep, which watches `scopes()` too — so it just
renders an empty history table, and starts fetching real near-real-time data
the moment `account_usage_copy_history` first gets a row for it (which
`ensure()` on the always-present activity scope keeps checking for on the
ordinary cadence). `fetch_page` still raises if `PIPE_HISTORY` is ever
requested with no resolvable target regardless — a genuine safety net for a
caller that skips this gate, not the expected path.

The two sources are merged in the view by `(FILE_NAME, LAST_LOAD_TIME)`, with
the near-real-time rows inserted last so they win on overlap — deliberately
not a date-based split (e.g. "recent from one source, older from the other"),
since that would hide real data whenever one source is thin or has failed
outright. Rows older than the "pipe history display window" setting are
filtered out of the *view* only; they stay in the fetch-window-bounded cache
regardless, so shrinking the display window later needs no refetch.

#### A scope whose data does not age on the ordinary cadence

`account_usage_copy_history_detail`'s backfill barely needs re-fetching at
all: once `INFORMATION_SCHEMA.COPY_HISTORY` covers the live last-14-days
window, the only thing a re-fetch of the `ACCOUNT_USAGE` side can surface is
data that has freshly aged *past* that 14-day window — which only moves once
a day. Ordinary scopes just use whatever `CACHE_TTL` the sweep passes in, but
forcing this one onto the same cadence would repeat the same round trip every
sweep tick for no new data.

Rather than build a new "background job that knows to skip itself" concept,
`models.scope_ttl(scope, default)` generalizes the existing TTL check itself:
a small `{ScopeKind: float}` override table, consulted from both places
staleness is ever decided — `Repository.stale_scopes` (the background sweep)
and `_ScopedView.ensure` (a view's own paint-time check) — so neither a view
nor a call site needs to know that one particular scope is special, and a
first-ever fetch (nothing cached yet) still happens immediately regardless.
It only ever *lengthens* the caller's TTL, never shortens it, so an
aggressively short `CACHE_TTL` setting is still honoured for every scope with
no override.

`PIPE_ACTIVITY_HISTORY`'s own override is 13 days, one short of
`INFORMATION_SCHEMA.COPY_HISTORY`'s 14-day retention rather than the full 14:
the two sources must jointly cover every day with no gap between them, and
`ACCOUNT_USAGE`'s own ingestion lag (up to a few hours) eats into a boundary
cut exactly at 14, so the override stays one day inside it for margin.
`PIPE_ACTIVITY` (the schema-wide latest-per-pipe snapshot the "Pipes (n)"
list reads) carries the same 13-day override, inherited from before this
scope was split in two rather than freshly justified by the argument above:
its own data changes every time any pipe in the schema loads something new,
which on a busy account is not rare, so the list's own Loaded/Status columns
can now go up to 13 days without a refetch even though nothing about the
`INFORMATION_SCHEMA` coverage argument requires that for *this* scope. That
was already true before the split -- `PipeListView` never merged in the
near-real-time source the way `PipeView` does -- so this isn't a regression,
but it's worth revisiting on its own if the list turns out to need fresher
data than that.

One landmine worth naming for whoever builds the next one: `object_kind` on
a `Scope` is not a general "this is about tables" marker — it specifically
means "this is a `SHOW`-listing request for one schema", and
`MainScreen.on_scope_needed` upgrades any scope carrying both a database and
an object kind into a whole-database `SHOW`-prime. A new SELECT-backed scope
that happens to also be about one object kind must leave `object_kind` unset
(`table_stats_scope`, `pipe_activity_scope` and the existing `columns_scope`
all do), or it gets silently redirected into fetching the wrong thing
entirely.

### Round trips dominate; payload is nearly free

Measured against the reference account: **~187 ms per `SHOW` round trip
regardless of size**, while 383 rows came back in 210 ms.

This inverts the intuitive approach to chunking. Small pages do **not** make
progress feel smoother — they multiply latency. Paging a 2145-table database in
pages of 5 would take ~80 s; in pages of 1000 it takes under a second. Pages
exist to **bound a job and give cancellation points**, not to animate progress.
Lowering the page size setting mainly makes things slower.

### Database priming

Because round trips dominate, a schema's objects are **not** fetched per
schema. One `SHOW <kind> IN DATABASE` returns every schema's objects at once —
measured at ~1 s for 2145 tables across 54 schemas, against ~10 s for 54
separate calls. Any request for one schema's objects is therefore upgraded to a
database-wide prime, which then completes *every* schema's scope from that
single result.

Schemas that returned nothing still get a completed pass. Otherwise they would
read as "never fetched", the tree would keep asking, and the node would never
settle.

> **Do not add keyset paging to the database-wide prime.** Object names are not
> unique across schemas — an `ORDERS` table in forty schemas is forty rows
> sharing one cursor value — so resuming after a name silently skips the rest
> of them. The prime therefore issues one unpaged statement and, if the result
> comes back at Snowflake's 10 000-row cap (truncated, and not safely
> resumable), falls back to fetching schema by schema. Within a *single*
> schema, where names are unique, keyset paging is verified correct and is
> used.

### Column cache priming

A table's columns are fetched lazily by default — one `SHOW COLUMNS IN TABLE`
per table, only when that table's detail view is opened (see "The Object
tree" and the registry section below). That leaves most tables in a database
with no cached columns at all, which is fine for the detail pane but means
column names would be absent from search for anything nobody has personally
opened — moot for the moment anyway, since column search itself is currently
disabled entirely (see "Known gaps"); this priming mechanism stays useful for
the detail pane and is ready to serve search again once that is re-enabled.

**Pre-warm cache** (Connect menu) fixes that on demand — there is no
automatic trigger, since there was no good signal found for "the user wants
this now" short of asking. Selecting it fires three background requests at
once for whatever database the cursor is currently under (multiple databases
can be selected, so this acts on the focused one, the same convention
`action_refresh`/`r` uses): the table and view object lists (via the
existing `DATABASE_PRIME` mechanism above) and a `COLUMNS_PRIME` scope. The
first two exist so the third has schema and table names to fall back to, not
as an afterthought — see below.

`COLUMNS_PRIME` mirrors `DATABASE_PRIME` one level deeper, in three tiers:

1. **Whole database, one round trip**: `SHOW COLUMNS IN DATABASE`, capped at
   the same 10 000-row `SHOW` limit.
2. **Truncated → one round trip per schema**: `SHOW COLUMNS IN SCHEMA`. This
   is the common case in practice, since columns multiply table count —
   a database that fit under the cap for `DATABASE_PRIME` can easily blow it
   here.
3. **A single schema still truncated → one round trip per table**: the
   already-existing lazy per-table fetch, just requested proactively instead
   of on open.

Unlike the object prime, a usable result needs no pre-seeding of empty
scopes: a real table always has at least one column, so there is no
"zero rows but still fetched" case to guard against the way an empty schema
needs one.

One cold-start edge case is handled directly rather than assumed away:
priming may be the first request ever made against a database nobody has
browsed this session, so the schema list the per-schema fallback needs to
iterate can still be empty. Rather than requiring the `SCHEMAS` scope to have
finished first — which would be the one dependency this architecture
otherwise has none of (see "Scopes: the unit of everything") — that fallback
asks Snowflake for the schema names directly when the cache has none, and
separately requests the `SCHEMAS` scope so the tree benefits too. The deeper
per-schema-truncated-too fallback is left depending on cache being warm by
then, an accepted gap: it only matters for one schema large enough to
overflow the cap by itself, and the menu action's own object-list priming is
already running concurrently by the time it could matter.

`SHOW COLUMNS IN DATABASE`/`IN SCHEMA` are unverified against a live account,
same caveat as the paging note below — the reference account was not probed
against a large enough number of columns to observe truncation or confirm
the exact result shape at that scale.

Priming always runs at **background** priority, the same tier as the
staleness sweep: it can run for a while with nobody watching, so it must
never jump ahead of interactive/visible work already queued, and it must
never trigger an unattended reconnect (`allow_reconnect=False`, same rule as
the sweep — see "The connection pool"). It draws on the same worker pool as
ordinary browsing rather than a dedicated connection, so it competes for
whatever is idle but can never grow the pool or lock out real interactive
work.

### Verified and unverified paging

`LIMIT n FROM 'name'` keyset paging was verified correct — same rows, same
order as an unpaged fetch — for databases, schemas and tables. It is
**unverified for views, pipes, stages and tasks**, because the probed account
had none of those to page through. The per-schema path uses it; the common
database-prime path does not, so the exposure is small.

---

## The connection pool

In a corporate account **every new connection can trigger an interactive MFA
prompt**. The pool exists to make that a one-time event.

- The connect dialog opens exactly **one** connection when submitted. There is
  no point asking about the rest before knowing that one even works, and a
  failed first connection is the common case the retry loop exists for.
- If the configured pool size is more than one, success is followed by a
  confirmation dialog ("Do you want to expand the connection pool to N?",
  where N is the configured pool size), with plain **Yes** / **No**
  options. Declining leaves the pool at size one rather than failing or
  silently upgrading — which is also what keeps a pool size of one always
  reachable on demand, handy under test and for a quick look that would
  rather not field extra MFA prompts.
- Confirmed growth, like the first connection, is opened **eagerly**, with
  progress reported as `Connecting (2/5) ...` / `(approve browser or MFA)` —
  the same two lines whether it is the lone up-front connection or growth
  confirmed afterwards, and always counted against the pool's eventual target
  size rather than however many connections that particular step happens to
  be opening.
- Every connection — the first and any confirmed growth — is opened
  **sequentially**. Opening them in parallel would fan out N browser windows or
  push notifications simultaneously — exactly the disruption this design
  exists to avoid.
- Sessions are kept alive, and the driver's SSO/MFA token cache is enabled, so
  the session does not expire mid-browse and re-prompt hours later, and
  connections 2..N usually do not prompt at all.
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
  **Pool gauge's total honestly shrinks** (e.g. `(2/4)` instead of `(2/5)`)
  to match what is actually alive. The gap this leaves is only ever repaired
  by the next interactive/visible checkout that finds the pool short — it
  opens the missing connection itself, on demand, restoring the total. A
  connection that fails only once actually queried (the case a closed-check
  at checkout cannot see) follows the same rule: replaced transparently for
  an interactive/visible caller, retried once against the fresh connection;
  dropped and shrunk, with no retry, for a background one.

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
> never persists anything, and the symptom is exactly the bug this
> architecture exists to avoid -- every connection in the pool, OAuth or SSO,
> reopens a fresh browser prompt instead of the second connection onward
> reusing what the first one just obtained. This was already latently true
> for `externalbrowser`'s "connections 2..N usually do not prompt at all"
> claim above; OAuth just made the gap loud enough to notice. Linux instead
> falls back to a permissioned file under `~/.cache/snowflake/`, which needs
> no extra dependency.
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

**snowcone stores no password anywhere.** Each remembered identity is a
`Profile` (`snowcone/profiles.py`, on disk at `recent.json`): account, user,
authenticator, role and warehouse, plus the databases last selected for that
identity's Object tree (see "The Object tree" above) -- UI state riding on
the same record because it is naturally scoped to the same identity, which is
also why the type outgrew being just a bundle of connection parameters. For
SSO and OAuth there is nothing to store -- the driver obtains and caches its
own credential; for password+MFA the driver's own token cache already means
the password is typed once per machine rather than once per connection.
Connections are recorded as "recent" only after they have actually worked --
but a database selection is written the moment the picker confirms one, live,
since there is no equivalent "did it work" gate for a UI preference the way
there is for a connection attempt.

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
- **Cancellable**: switching database drops queued work for the old one. A page
  already in flight cannot be interrupted, but its continuation is suppressed.

Results reach the UI as a message naming **one scope**, which the main screen
**routes** to the widget showing it — never broadcasts. An update to one
schema's tables cannot cause a whole-tree rebuild.

### The staleness sweep

A slow timer asks which of the scopes **currently on screen** have exceeded the
cache TTL, and requests those at background priority. Scopes nobody is looking
at are never refreshed; anything urgent was already requested interactively.
A TTL of `0` disables the sweep entirely.

---

## The Object tree

Lazy: a node's children are materialised when it is first expanded. Counts on
collapsed nodes (`Schemas (54)`, `Tables (383)`) come from the cache, so they
are correct and instant without materialising anything beneath them.

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
path** — node ids and line numbers are meaningless across a rebuild. Expansion
state is carried across too; silently collapsing everything loses the user's
place just as surely as moving the cursor.

There is no insert-at-index in Textual's tree API. That single gap is why tier
3 exists at all.

One more constraint that bites repeatedly: a freshly created node has no line
number until the next refresh, so **any cursor move onto newly created nodes
must be deferred by one refresh** or it silently lands on the first row.

---

## Detail views: the registry

The Detail pane hosts **one Object view at a time**, looked up by object kind
from a registry that views add themselves to at import time.

Not a switcher holding every view mounted: with a growing set of kinds that
means a dozen widget trees resident at all times, each needing a coherent
"nothing selected" state and each subscribing to updates it does not care
about. Mounting on demand keeps exactly one view alive.

**The swap is on object *kind*, not on object.** Moving between two tables
re-points the existing view, so the widget, its columns and its scroll position
all survive — which is what makes arrowing down three hundred tables cheap. A
different kind is a genuine change of shape, so that view is replaced.

**A view owns its whole body.** There used to be a second registry contract --
a view's `compose_context()` fed a separate "Context bar" pane above the
Detail pane -- but that pane was never a distinct concept, only
selection-dependent content cramped into a one-line strip by its own
container. The Context bar and the Detail pane are now one pane, and
`compose_context()` is gone with it: whatever a view wants to show is
content it composes as part of its own body, wherever in that body it
belongs, with no second registry to keep in sync.

A view declares which cache scopes it reads. That is how it knows whether an
update concerns it, and how its data joins the staleness sweep.

An object kind with no registered view shows a placeholder rather than
crashing, so the tree can list kinds ahead of their views existing.

The kinds are genuinely different shapes, which is the justification for the
seam: a table shows columns; a pipe has no columns at all, so its body is a
`TabbedContent` instead -- **Load history** (see "A second source" for where
that comes from, plus a detail panel below the table for whatever the
currently highlighted row wants to say about itself -- today that's a load
error's message and line number, if it has one) and **Definition** (the
pipe's raw `COPY INTO` SQL, unformatted -- no syntax highlighting, a
deliberate scope boundary for now). The two tabs are also keyboard-mnemonic
switchable (`l`/`d`, hidden from the footer), the same pattern sibling
project snakecam's `JobDetailsScreen` already established.

The "Pipes (n)" category node (`PipeListView`) is tabbed the same way, for
the same reason a single pipe's own view is not: **Status** (name,
running/stopped, and `invalid_reason` as three columns, one row per pipe --
a stopped pipe is otherwise indistinguishable from a healthy one) needs
every pipe in the schema to be useful, not just whichever one happens to be
open, so it lives on the list view instead. It opens by default (mnemonic
`s`), alongside **Load history** (mnemonic `l`) -- last load activity per
pipe, the same data this view already showed before it had tabs.
`invalid_reason` costs no new scope here either: it is already mirrored onto
the same `SHOW PIPES` rows this view reads for the Name column.

---

## Search

A modal (`SearchScreen`), opened by `/` at roughly 90 % of the screen and
closed by `escape` (dismisses with nothing chosen) or `enter` (opens the
highlighted hit and closes). It is a modal rather than an always-mounted
widget in the top pane because search is the primary way to navigate a
database of thousands of objects, and it earns the room only a modal can take
without permanently stealing it from the tree and detail pane.

Find-as-you-type over every cached object and schema across every selected
database, ranked by subsequence matching so `stgord` finds
`STG_CUSTOMER_ORDERS`. Substring search would not, which is the whole point;
sqlite's full-text matching cannot express it either, so ranking happens in
Python over three in-memory corpora — object names, column names (paired with
their owning object), and schema names. Column search is currently disabled
— see "Known gaps" — but the corpus and its ranking logic below are otherwise
described as still live, since the code is unchanged, only its one call site
in `SearchScreen.on_mount` is commented out.

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
schema lists are cheap (~5ms even at 2000 objects), but the column corpus is
real work: measured at ~55-75ms for 30k cached columns, and ~700ms at 270k.
Fetching all three together would mean the cheap ones wait on the slowest —
splitting them means name and schema matches are usable almost immediately,
with column matches joining in once their own load lands (when that load
runs at all — see "Known gaps": column search's task creation is currently
commented out, so in practice only two of the three loads fire).

Typing never waits on a load either: ranking proceeds against whatever
corpus already exists — empty, partial, or complete — and each load re-runs
the current query itself once it finishes, so the list corrects itself in
place rather than a keystroke ever blocking on it. **Enter is the one
exception**: choosing a result and closing the dialog is a commitment a
still-partial corpus should not get to make, so that path alone waits for
every load that is actually running first.

None of the three corpora carry live staleness tracking, for the same reason:
there is no widget left mounted for a cache update to mark them stale while
the dialog is open. `SnowconeApp` routes `CacheUpdated` to `self.screen`,
which while the modal is open is the modal itself, not `MainScreen`, so a
background update would not reach a live corpus here any more than it
reaches the tree or detail pane while any other modal (the database picker,
say) is on top. Closing and reopening the dialog picks up whatever changed in
between, at the cost a keystroke used to pay to notice a stale corpus.

### Matching and ranking

Object matching scores **both `SCHEMA.NAME` and the bare name**, keeping
whichever is better, with a small preference for the name. This is not
gold-plating: Snowflake schemas repeat themselves in their objects' names —
a `STAGING` schema full of `STG_` tables — and greedy left-to-right matching
otherwise spends the query's leading characters inside the schema, leaving a
poor alignment in the name. Without this, `stgord` ranks `STAGING.LOAD_ORDERS`
*above* `STAGING.STG_CUSTOMER_ORDERS`.

The displayed text also carries a `DATABASE.` prefix ahead of `SCHEMA.NAME`,
since several databases can be selected at once now — but that prefix is
added purely at render time (`SearchScreen._option_text`) and deliberately
excluded from ranking itself. Extending the schema/name qualification to a
third, database level would spend the query's leading characters on the
database too, for no benefit: there is no database-repeats-inside-object-name
ambiguity the way `STAGING`/`STG_` justifies it for schema. Don't assume the
database participates in match scoring the way the schema does — it doesn't.

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
scores against every cached column for little relevance benefit and, at a
heavily pre-warmed scale, real per-keystroke latency: measured at 270k cached
columns, ranking a single query ranged 73-450ms depending on how selective it
was. Even so, this only skips queries shorter than the constant — a 4+
character fragment still costs whatever ranking the full column corpus costs;
the constant does not bound that.

Ranking runs in a thread (`asyncio.to_thread`), not on the event loop.
Measured cost for a whole corpus, worst query: 2k objects 3ms, 10k 14ms, 50k
70ms, 100k 150ms — comfortable directly on the event loop at those sizes, and
the input is debounced on top, which is how this ran until the column corpus
routinely started outnumbering the object corpus 10-20x. The matcher was
already a plain function precisely so this stayed an easy change.

### Choosing a hit

Choosing an object or column hit **reveals it in the tree** (`ObjectTree.
reveal`) as well as opening it in the detail pane, so search leaves you
somewhere you can keep browsing from. A **schema** hit has no detail view to
open — `SchemaRef` carries only `database`/`name`, deliberately not an
`ObjectRef` with some placeholder kind, since a schema has no `ObjectKind`
and no mirror row of its own to show. Choosing one instead **reveals and
expands it** in the tree (`ObjectTree.reveal_schema`) and stops there.

---

## Status footer

The right corner carries four indicators. Textual's command-palette binding is
suppressed so the corner is available; the footer is otherwise Textual's, so
key bindings keep working without being re-implemented.

- **Role** / **Warehouse** — the pool's current role and warehouse, read
  straight off the pool's connect params each tick. Leaving either field blank
  in the connect dialog does not mean "none" is shown forever: Snowflake
  always activates *some* role for a session (the user's default, or
  `PUBLIC`), and the driver already learns what it resolved from the login
  response (`sessionInfo.roleName` / `.warehouseName`, exposed as
  `connection.role` / `.warehouse`) — no extra query needed. `connect_all`
  adopts whichever of the two was left blank from that
  (`ConnectionPool._adopt_resolved_session`), so the footer shows the real
  session state rather than an unfilled form field. Warehouse can genuinely
  read `none` even after this — a session can have no active warehouse at all
  (see "`SHOW`, because it needs no warehouse" above) — but role never
  legitimately does. Picked from **Connect > Role ...** / **Warehouse
  ...**, the same fuzzy-picker-over-cached-rows pattern as the database
  picker (`PickerScreen`, single-select -- the database picker is the
  multi-select sibling, `MultiPickerScreen`, see "The Object tree" above),
  backed by their own cache scopes (`SHOW WAREHOUSES`, `SHOW ROLES`
  — small, flat, unpaged lists, unlike the object mirrors), requested
  eagerly on mount alongside the database list rather than on the picker's
  first open — lazily fetching it there meant the first open ever just
  requested the list and closed again, since there was nothing cached yet
  to show. Unlike the
  database picker, which only ever changes what a query is built to ask for,
  picking a warehouse or role actually rewrites the session: see
  `ConnectionPool.set_role` / `set_warehouse` below.
- **Countdown** — seconds until the data *currently on screen* goes stale: the
  TTL minus the age of the scope behind the cursor. It is not a global timer;
  it changes as you navigate, and for an object leaf it reports the list the
  object came from, since a leaf owns no scope of its own. It becomes a spinner
  while that scope is being refetched, and blanks when the TTL is `0`. Fixed
  width, right-aligned, no leading zeroes, so the digits do not shift as it
  counts down.
- **Pool gauge** — `(in use / total)` connections, giving a live sense of
  background activity.

The gauge is polled a few times a second rather than pushed from the pool. Pool
usage changes on worker threads, so pushing would require marshalling on every
acquire and release; polling a lock-guarded integer is simpler, has no thread
hazards, and is still fast enough to register a ~187 ms round trip. The
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
cache file per identity, not a role dimension on Scope" earlier in this
document for why, and for the fetch-service sequencing a same-pool role switch
requires. Warehouse switches skip all of this: `SHOW` is metadata-layer and
consults no warehouse, so nothing about the cache needs to change when it does.

---

## Settings

Settings are declared once and self-register, so a settings UI can enumerate
them with no per-setting wiring. Only values that differ from their default are
persisted -- a setting nobody has touched stays absent from the file, so a
future release can change its default without a stale override shadowing it.
Values are read **at the point of use**, never cached, so a change takes effect
without a restart.

All of that machinery -- `Setting`, the registry, `ConfigStore`, the dialog --
is `tcw`'s (see "Shared widgets" below). `snowcone/settings.py` is only the
declarations plus the one `configure("snowcone")` call that tells the shared
store which app's config file to use; the store resolves that path lazily, on
first read, so importing the module touches no disk.

The ones that matter: **cache TTL** (`0` disables background refreshing),
**pool size**, **fetch page size**.

**Settings ...** (Connect menu) opens a modal listing every registered
`Setting`, grouped under a heading per `Setting.group`, each editable in
place. **OK** validates and writes, then closes; **Apply** does the same but
leaves the dialog open; **Cancel**/escape discard, confirming first if
anything was typed that differs from what was last saved -- the same "unsaved
changes" guard `ConnectScreen` uses. Clearing a field's text and confirming is
how a setting is reset to its default. A value that fails to parse
(`ValueError`) blocks OK/Apply with an inline warning rather than silently
discarding it or falling back to the default -- that fallback exists for a
hand-edited settings.json, not for a mistake made in the dialog itself.

`tcw`'s dialog also supports per-instance settings and callback-only action
rows, which snowcone declares none of; they exist for the sibling app the
dialog originally came from.

---

## Shared widgets

The menu bar, the settings system, and the dialog chrome underneath both
(`DialogScreen`, `ConfirmScreen`) live in **`tcw`** ("Textual Common
Widgets"), a sibling project shared with `snakecam` and `vc`. They were
extracted because the same code had been copied into three apps and had
already started to drift.

It is consumed as an editable path dependency (`[tool.uv.sources]` in
`pyproject.toml`), so an edit there is live here with no reinstall.

> **`tcw` must never appear in `[project.dependencies]`.** It is not published
> anywhere, so a built snowcone wheel instead bundles a copy of its sources
> directly (`hatch_build.py`, triggered as a custom build hook on both the
> `sdist` and `wheel` targets — see the hook's own comments for how it locates
> a source copy for each). `[tool.uv.sources]`'s path override only takes
> effect inside this project's own `uv sync`/lock resolution; it does nothing
> for a plain `pip install`/`uv pip install` of the built wheel elsewhere. If
> `tcw` were listed as a real dependency, any such install would resolve it
> against PyPI instead — where an unrelated, same-named package already
> exists — silently shadowing the bundled copy with the wrong one. `tcw` is
> therefore listed only under `[dependency-groups] dev`, which `uv sync`
> honors for the local venv but which never becomes part of a built
> distribution's metadata.

Three things about it are worth knowing from inside snowcone:

- **`tcw` ships its TCSS inline**, as `DEFAULT_CSS` on each widget, rather
  than in a stylesheet -- a library cannot rely on a consumer's `CSS_PATH`.
  So the menu-bar and settings-dialog *layout* rules are no longer in
  `app.tcss`. Colour is not tcw's business, though: it ships no theming, so
  `Button.button-gray` stays here, and the gray treatment for the buttons
  inside tcw's own dialogs is applied by id (`#confirm-no`,
  `#settings-apply`, `#settings-cancel`) rather than by a class, since there
  is no call site here to hang one on. `app.tcss` outranks `DEFAULT_CSS`
  regardless of specificity, so anything tcw does style can still be
  overridden here.
- **`MenuBar` installs its own `F9` binding** on whichever screen it is
  mounted on, which is why `MainScreen.BINDINGS` no longer carries one.
  Textual only consults the focus chain plus the screen and the app when
  resolving a key, so a bar that is never focused cannot own the binding
  itself.
- **A `Menu`/`MenuItem` id is also a CSS id.** Textual only requires it to be
  unique among one menu's own children, but any `app.tcss` rule of the same
  name styles it too -- and for a widget inside a one-row bar that does not
  show up as a wrong colour, it shows up as a garbled row of scrollbar
  blocks. The Connect menu is `#connect-menu` rather than `#connect` for
  exactly this reason: `#connect` is the connect dialog's container.
  `test_menubar.py` asserts the title's *geometry*, not just its text, so a
  future clash fails a test instead of only looking wrong.

---

## Tooling and conventions

- **uv** manages the project and the virtualenv. Use `uv sync --all-groups` for
  dependencies and `uv add` to introduce one. The interpreter is the project
  `.venv`. `tcw` is an editable checkout of a sibling directory rather than a
  released package, and is deliberately not a real dependency of the built
  package -- see "Shared widgets".
- **ruff** formats and lints, on **default settings with no configuration** —
  there is deliberately no `[tool.ruff]` section. Run `ruff format` and
  `ruff check` after changes and let the formatter decide. ruff is installed
  globally, not as a project dependency.
- **pytest** with async auto mode; every UI test drives a real app through
  Textual's test pilot.
- **No test may reach the network or import the Snowflake driver.** A fake
  gateway and a fake connector stand in, so the cache, scheduler and entire UI
  are driven offline. The gateway is a protocol precisely to make that seam
  exist.
- Modules outside the UI layer **do not import Textual**. That is what keeps
  the cache, scheduler, matcher and formatting testable as plain functions.
- Comments explain *why the obvious alternative was rejected*, and tests are
  named as sentences describing the behaviour they protect.

---

## Known gaps

- Paging is unverified for views, pipes, stages and tasks (see above).
- `SHOW COLUMNS IN DATABASE`/`IN SCHEMA` (column cache priming) are likewise
  unverified against a live account at scale.
- `information_schema_tables`'s `LAST_DDL`/`LAST_ALTERED` columns are
  documentation-only, unverified against a live account, same caveat as the
  two gaps above.
- `account_usage_copy_history`'s and `account_usage_copy_history_detail`'s
  columns are likewise documentation-only, unverified against a live
  account. They additionally need `IMPORTED PRIVILEGES` on the `SNOWFLAKE`
  database (or `ACCOUNTADMIN`) and can lag real pipe activity by up to ~2
  hours — neither has been confirmed against a live account either, so the
  "Pipes (n)" list's activity columns, and a single pipe's older load
  history, may render blank on an account without that grant even though
  everything else here works. The `QUALIFY ROW_NUMBER() OVER (PARTITION BY
  PIPE_NAME ORDER BY LAST_LOAD_TIME DESC) = 1` that reduces
  `account_usage_copy_history` to one row per pipe is likewise unverified
  against a live account -- confirmed only by a hung, hand-tested account
  with the *unreduced* query, not by re-running the reduced one against it.
- `information_schema_copy_history`'s columns are likewise documentation-only
  and unverified, including the shape of its `PIPE_NAME` output column
  (assumed bare/unqualified for the `WHERE PIPE_NAME = %s` filter in
  `queries.pipe_history`) and whether Snowflake accepts a fully-qualified
  `catalog.schema.table` string for its `TABLE_NAME` argument the way
  `Repository.pipe_target_table`'s result is used to build one. Either
  assumption being wrong would surface as a single pipe's near-real-time
  load history rendering empty while the `ACCOUNT_USAGE`-backed older
  history keeps working — not a crash, per this codebase's usual
  degrade-blank convention, but worth a real-account check before relying
  on it.
- `FIRST_ERROR_MESSAGE`/`FIRST_ERROR_LINE_NUMBER` on `account_usage_copy_
  history`/`information_schema_copy_history` are likewise documentation-only,
  unverified against a live account -- same caveat as the two gaps above,
  now covering two more columns on both sources.
- The tree/Detail drag-resize (see "Screen anatomy") does not persist across
  sessions, and does not re-clamp an already-narrow pane if the terminal is
  then resized smaller -- both accepted, ported as-is from sibling project
  snakecam's identical mechanism, not engineered around.
- Stages and tasks appear in the tree but have no Object view yet; they show a
  placeholder.
- **Multiple databases can now be browsed and searched at once** — this
  reverses what used to be a deliberate constraint here ("cross-database
  browsing is out of scope by design"). The database picker (`ctrl+d`) is a
  multi-select modal (`MultiPickerScreen`), each selected database gets its
  own top-level node in the Object tree showing its schema count directly,
  and search spans every selected database. The concern the old constraint
  was protecting against — total object counts across many databases getting
  impractical — is not addressed by anything new here; it simply hasn't been
  hit yet at the scales this has been exercised against. Treat performance at
  a large multi-database selection as an open question for a future session,
  not one this change already answered.
- Databases still aren't themselves a *searchable hit* the way a schema is —
  a schema hit reveals and expands it in the tree via `SchemaRef`; there is
  no equivalent for a database, since selecting or deselecting one is a
  picker action, not a search result. This part of the old constraint stands.
- Column search across selected databases is implemented at the repository
  layer (`Repository.search_column_corpus`, `Sequence[str]`-scoped and ready)
  but its one call site in `SearchScreen.on_mount` is commented out, not
  deleted — cross-database column search specifically was judged not ready
  for prime time yet. Re-enabling it is restoring one call, not writing new
  code.
- There is no automatic trigger for column cache priming — no signal was
  found for "the user wants this now" short of asking, so it is a manual
  menu action rather than something that runs on its own.
