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
│ │   Object tree   ││       (hosts exactly one detail view;               │
│ │                 ││   drag this border to resize either pane)           │
│ │                 ││                                                     │
│ └─────────────────┘│                                                     │
├────────────────────┴─────────────────────────────────────────────────────┤
│ F1 Log  / Search  F5 Refresh                       Role  Warehouse  (2/5)│
└──────────────────────────── Status footer ───────────────────────────────┘
```

`F1` slides the **Log panel** down over the top third, showing every
statement snowcone sends; `F1` again hides it. See "The SQL log".

`/` opens the **Search modal** over this screen, at roughly 90 % of the
terminal:

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ┌────────────────────────────────────────────────────────────────────┐   │
│ │ search objects -- more words narrow by schema or database           │   │
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
| **Menu bar** | Midnight-Commander style: hidden except while in use, opened by `F9` or by clicking a title. The bar itself -- the staged bar-only/dropdown flow, mnemonics, separators, the `F9` binding -- is `tcoda`'s `MenuBar` (see "Shared widgets" below); snowcone only declares what is in it, inline in `MainScreen.compose`. Two menus. **Snowcone** (`s`), the app-identity menu, leftmost: **About ...** (`a`) -- a `tcoda.widgets.AboutMenuItem` reading `ui/about.txt` (see "Shared widgets" below for why that's a real filesystem path, not just an `importlib.resources` handle) -- then a separator, **Settings ...** (`s`) -- a `tcoda.widgets.SettingsMenuItem`, which opens the dialog itself, so snowcone wires nothing for it -- then another separator and **Quit** (`q`, `Ctrl+Q` shown right-aligned), which runs `app.quit`. **Connection** (`c`), the session menu: **Open...** (`o`, `Ctrl+O` shown right-aligned) to reopen the connect dialog and swap in whatever pool it returns, then a separator, **Databases ...** (`d`, `Ctrl+D` shown right-aligned) to open a **multi-select** fuzzy picker (`MultiPickerDialog`, see "The Object tree" below) over cached databases, **Warehouse ...** (`w`, `Ctrl+W`) and **Role ...** (`r`, `Ctrl+R`) to open the still-single-select fuzzy picker (`PickerDialog`) over cached warehouses/roles, applying the choice session-wide (see below), and **Pre-warm cache** (`p`) to fill the column cache of whatever database the cursor is currently under, in the background -- see "Column cache priming". Choosing an entry posts `MenuItem.Pressed`, handled with `@on(MenuItem.Pressed, "#database")` on `MainScreen`, which hands off to the same `@work` wrapper the matching key binding uses -- a plain message handler has no worker context, and `push_screen_wait` needs one. A `shortcut` label advertises a key binding that already does the same thing, independent of `mnemonic` (which drives in-menu letter selection). `Ctrl+O`/`Ctrl+D`/`Ctrl+R`/`Ctrl+W` all stay live as key bindings but are hidden from the footer (`show=False`) -- the menu's `shortcut` label is where they're advertised, keeping the footer down to the bindings someone would reach for without having opened the menu first. |
| **Object tree** | The browser. One top-level node per selected database, each showing its own schema count directly (`MYDB (12)`) and expanding straight into schemas -- → schema → `Tables (n)` / `Views (n)` / … → object. No border title: several databases can be selected at once, so there is no longer one canonical database to name there. `ctrl+d` opens the **Databases picker**, a multi-select fuzzy modal over cached databases -- every selection gets its own top-level node, and search (below) runs across all of them. |
| **Search modal** | Find-as-you-type over every cached object and schema across every selected database (column search is currently disabled, see "Known gaps"), opened by `/` at roughly 90 % of the screen -- search is the primary way to navigate a large database, so it gets a screenful of ranked matches rather than a sliver of the top pane. `enter` opens the highlighted hit and closes the modal; `escape` closes it without choosing. Choosing a hit reveals it in the tree as well as opening it, so it leaves you somewhere you can keep browsing from. |
| **Detail pane** | Hosts exactly one detail view at a time, chosen by whatever node the cursor is on. Its shared right border with the Object tree is mouse-draggable, resizing both panes live. |
| **Object view** | The per-type detail UI for one object (e.g. Table view, Pipe view). Owns its whole detail body. One of four view families the pane can host -- see "Detail views" below for the other three. |
| **Status footer** | Key bindings on the left; **Role**/**Warehouse** labels, **Countdown** and **Pool gauge** in the right corner. Textual's command-palette key is suppressed so the corner is ours. |
| **Log panel** | Hidden until `F1`, then slides down over the top third of the screen: every statement snowcone sends, live. `tcoda`'s `LogViewer`, fed by `gateway/sql_log.py` -- see "The SQL log" below. |

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

- a **uniform object view** used by search, flattening every type to
  `(database, schema, name, kind)` — `kind` there is Snowflake's own column
  name, kept because this layer still mirrors; snowcone's own word for the
  same idea is `ObjectType` (see "Vocabulary" below);
- the **freshness ledger**, below.

Bookkeeping columns are underscore-prefixed so they can never collide with a
Snowflake column name.

### Vocabulary: Snowflake's words, and where snowcone's differ

Snowflake's own terms are used throughout, in identifiers as well as prose,
and column headers follow the same rule (see "Column headers mirror
Snowflake"). Where snowcone needs a name Snowflake has none for, it is
obviously snowcone's: `Scope`, `CacheState`, `NodeType`, the `_`-prefixed
bookkeeping columns.

The one place the two collide is **type**. Snowflake's docs, `GRANT ... ON
<object_type>`, `INFORMATION_SCHEMA.TABLES.TABLE_TYPE` and Snowsight's own
filters all call "table vs view vs pipe" an *object type*, so that is what
`ObjectType` is called here. But several `SHOW` results also carry a column
literally named `kind` — `SHOW DATABASES` returns `STANDARD`/`IMPORTED
DATABASE` in it, `SHOW TABLES` a per-row detail — and those are mirrored
under that name, unchanged, because the mirror layer never renames anything.
So `kind` in `schema.sql`, in `search.sql` and in a `TableSpec.columns` tuple
is always Snowflake's column; `type` everywhere else is snowcone's word for
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
dependency graph, no ordering constraint and no deadlock surface. If a schema
really was dropped, its tables fetch fails with "does not exist", that failure
is recorded, and the failing job asks for the listing one level up
(`models.containing_scopes`), whose refresh removes the node. That request
is made after the fact and never waited on, so it adds no ordering.

This used to say the schema list's "own next pass" would remove the node,
and that was true but useless: the next pass was the TTL sweep, up to
`CACHE_TTL` away and postponed by every navigation, so a schema dropped in
Snowsight stayed in the tree and failed every visit with an error toast. A
drop made elsewhere is invisible to snowcone until a statement trips over it,
so tripping over it is the trigger. "Does not exist" is recognised by the
driver's error number (2003), not its message text, which varies by object
kind; it is reported as a warning rather than an error, and identical
notices are folded, since one visit to a dropped schema fails its Grants and
Tags together. It climbs one level per failure: a table's columns name only
the table, so a dropped *schema* found that way first refreshes the schema's
object listings, and those are requested per schema on purpose -- a
database-wide prime would succeed and leave the schema node standing, while
`SHOW ... IN SCHEMA` fails the same way and carries the refresh up.

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
table, not one scope type, all of it — is only ever valid for the role that
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
own. Adding the column meant editing `schema.sql` in place, which means
deleting an existing cache file — the second time that trade has been taken,
after `FIRST_ERROR_MESSAGE`.

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
each field — only the source changed. That holds for `DATA_TYPE`'s values too:
SHOW's JSON reports two internal categories, `FIXED` and `REAL`, which are
stored as the `NUMBER` and `FLOAT` that `INFORMATION_SCHEMA` reports. `FIXED`
is not valid DDL, so keeping it would put a type name in the cache that no
Snowflake statement accepts. A live comparison of all three sources (SHOW,
`INFORMATION_SCHEMA`, `DESC TABLE`) found no other differences in the type
name. A type that cannot be parsed degrades to
"unknown"; a column snowcone cannot describe is still a column worth listing.

### A second source: one new mirror table per SELECT, never a retrofit

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

**The shape rule: never retrofit a column onto an existing mirror table.**
`schema.sql` is all `CREATE TABLE IF NOT EXISTS` with no migration system, so
an added column silently never appears in an existing cache file and the
first write to it fails. Each new source instead gets a brand-new mirror
table, named and shaped like the view it reads (upper-case columns for an
`INFORMATION_SCHEMA`/`ACCOUNT_USAGE` source — the same exception
`information_schema_columns` established).

There is one deliberate exception on record, and it is not a counterexample:
`FIRST_ERROR_MESSAGE`/`FIRST_ERROR_LINE_NUMBER` were added straight onto both
copy-history tables. snowcone is pre-1.0 with no migration system and no
backward-compatibility policy, so the usual objection was answered the
cheaper way — `schema.sql` was edited in place and anyone with an existing
cache file deletes it. A new table stays the right move whenever "delete your
cache" is not an acceptable answer.

#### The second-source scopes

| Scope | Source | Feeds | Needs |
|---|---|---|---|
| `TABLE_STATS` | `INFORMATION_SCHEMA.TABLES`, `TABLE_TYPE = 'BASE TABLE'` | Tables (n): Last DDL, Last Altered | warehouse |
| `VIEW_STATS` | the same statement, `TABLE_TYPE = 'VIEW'` | Views (n): Last DDL, Last Altered | warehouse |
| `MATERIALIZED_VIEW_STATS` | `SHOW MATERIALIZED VIEWS` | Views (n): Rows, Size | — |
| `PIPE_ACTIVITY` | `ACCOUNT_USAGE.COPY_HISTORY`, latest row per pipe | Pipes (n) activity columns; target-table resolution | warehouse, `IMPORTED PRIVILEGES` |
| `PIPE_ACTIVITY_HISTORY` | the same view, one pipe, unreduced | Pipe view Load history (older half) | warehouse, `IMPORTED PRIVILEGES` |
| `PIPE_HISTORY` | `INFORMATION_SCHEMA.COPY_HISTORY` | Pipe view Load history (recent half) | warehouse, a resolved target table |
| `TASK_ACTIVITY` | `ACCOUNT_USAGE.TASK_HISTORY`, latest row per task | Tasks (n): Last Scheduled, Last Run State | warehouse, `IMPORTED PRIVILEGES` |
| `TASK_ACTIVITY_HISTORY` | the same view, one task, unreduced | Task view Run history (older half) | warehouse, `IMPORTED PRIVILEGES` |
| `TASK_HISTORY` | `INFORMATION_SCHEMA.TASK_HISTORY` | Task view Run history (recent half) | warehouse |
| `GRANTS` | `SHOW GRANTS ON SCHEMA`/`ON DATABASE` | Schema/Database view Grants | — |
| `TAG_REFERENCES` | `INFORMATION_SCHEMA.TAG_REFERENCES` | Schema/Database view Tags | warehouse |
| `COLUMN_TAG_REFERENCES` | `INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS` | Columns view Tags column + detail panel | warehouse |

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
with row count" rule the rest of this app already holds `SHOW` to.

Snowflake's own visibility rules still apply on top: `SHOW GRANTS ON <object>`
only returns what the active role owns, has `MANAGE GRANTS` on, or reaches
via `ACCOUNTADMIN`/`SECURITYADMIN`, so a short or empty Grants tab under a
lesser role is correct, not an incomplete fetch.

#### One row per pipe, in SQL — a real incident

`PIPE_ACTIVITY` is reduced to each pipe's latest load by a `QUALIFY
ROW_NUMBER() OVER (PARTITION BY PIPE_NAME ORDER BY LAST_LOAD_TIME DESC) = 1`,
and that `QUALIFY` is load-bearing. Without it, on an account with ~400 pipes
and frequent ingestion, the query returned unboundedly many historical rows —
one per load, over the whole fetch window, across every pipe in the schema —
and the app hung for minutes materialising them just to throw away all but
the newest per pipe a moment later. Collapsing in Python is not enough; it
has to happen in SQL. `TASK_ACTIVITY` carries the same reduction for the same
reason.

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
under a different `_scope` value, because those tables' primary keys predate
having two scopes write into them and do not include `_scope`: a row visible
to both would let whichever pass wrote it last steal its `_scope` tag from
the other.

#### Merging the two halves of a history

A pipe's or task's history merges an `ACCOUNT_USAGE` backfill with a
near-real-time `INFORMATION_SCHEMA` source, the latter inserted last so it
wins on overlap. Deliberately **not** a date-based split ("recent from one,
older from the other"): that would hide real data whenever one source is thin
or has failed outright, whereas overlaying needs no reasoning about which
scope succeeded.

The merge key differs, and the reason is Snowflake's: a task run has
`QUERY_ID`, Snowflake's own identifier for one run, present in both sources.
A pipe's file load has no equivalent, so it is keyed on `(FILE_NAME,
PIPE_RECEIVED_TIME)`.

`LAST_LOAD_TIME` looks like the natural second half of that key and is not,
and this was a real incident: the two views stamp it independently, so the
same load carried `11:11:24.961` in `ACCOUNT_USAGE` and `11:11:24.926` in
`INFORMATION_SCHEMA` on a production account. Every load in both sources
showed up twice, which the minute-precision "Loaded" column hid. Comparing all
shared columns for every overlapping load there found `LAST_LOAD_TIME` the
only one that differed, and `PIPE_RECEIVED_TIME` equal and non-NULL in every
pair. A row without one falls back to `LAST_LOAD_TIME`, so it cannot collapse
several loads of one file name into one.

Rows older than the "display window" setting are filtered out of the *view*
only; they stay in the fetch-window-bounded cache, so shrinking the display
window later needs no refetch.

#### Resolving a pipe's target table, and the pipe that never loaded

`INFORMATION_SCHEMA.COPY_HISTORY` wants a `TABLE_NAME`, and Snowflake exposes
no way to ask it for "this pipe's" history. Rather than parse the target out
of the pipe's `COPY INTO` text, `Repository.pipe_target_table` reads it off
whatever `PIPE_ACTIVITY` already has cached for that pipe — Snowflake's own
resolution, already being fetched for the Pipes (n) list, not a guess. That
is an opportunistic read of a sibling scope's cache, the same pattern
`_fan_out_columns_by_schema` uses for `SCHEMAS`, not a dependency between
scopes.

A pipe that has never loaded anything has no cached row to resolve a target
from, and that is a normal, **permanent** state for plenty of real pipes, not
a transient failure to retry. Treating it as one was the first version's
mistake: it queued `PIPE_HISTORY` anyway, `fetch_page` raised, and the fetch
layer reported an error on every attempt, forever. The fix lives above the
fetch layer: `PipeView.scopes()` only lists `pipe_history_scope` once a
target actually resolves, and `update_content` only `ensure()`s it under the
same condition — so neither the interactive path nor the background sweep
(which watches `scopes()` too) ever queues it. Such a pipe renders an empty
history and starts fetching real data the moment `PIPE_ACTIVITY` first gets a
row for it. `fetch_page` still raises if `PIPE_HISTORY` is requested with no
target: a safety net for a caller that skips the gate, not the expected path.

`TaskView` needs none of this. `INFORMATION_SCHEMA.TASK_HISTORY` accepts
`TASK_NAME`/`DATABASE_NAME`/`SCHEMA_NAME` as direct named arguments, all of
which a task's own `ObjectRef` already carries, so both its history scopes
are always requested once the view is open.

One thing that looks redundant and is not: `queries.task_history` still
database-qualifies the function name (`"<database>".INFORMATION_SCHEMA.
TASK_HISTORY(...)`) even though `DATABASE_NAME` is also one of its named
arguments. Those are two different things, and this was a real incident: an
unqualified `INFORMATION_SCHEMA.TASK_HISTORY` resolves against whichever
database is current in the session, which the pool never sets (only `USE
ROLE`/`USE WAREHOUSE` are ever issued), so it failed on a live account with
"invalid identifier" regardless of where the task lived — silently, since the
`ACCOUNT_USAGE` half of the merge still returned data. `queries.pipe_history`
qualifies `COPY_HISTORY` the same way.


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
a small `{ScopeType: float}` override table. It only ever *lengthens* the
caller's TTL, never shortens it, so an aggressively short `CACHE_TTL` setting
is still honoured for every scope with no override, and a first-ever fetch
(nothing cached yet) still happens immediately regardless.

`scope_ttl` is reached only through **`models.is_stale(scope, age, ttl)`,
which is the one place staleness is decided anywhere in the app.** All three
callers go through it: `Repository.stale_scopes` (the background sweep),
`_ScopedView.ensure` (a detail view's paint-time check) and
`ObjectTree._ensure` (the tree's). Having one function is not tidiness — the
three had already drifted, with the tree checking a `CACHE_TTL` snapshotted
into it at construction (so a settings change did nothing until a restart)
and skipping `scope_ttl` altogether.

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

`object_type` on a `Scope` means "this is a `SHOW` listing of one object
type", and nothing else keys off it: `MainScreen.on_scope_needed` upgrades a
scope to a whole-database `SHOW`-prime when its *type* is `OBJECTS`, not when
it happens to carry both a database and an object type. That distinction used
to be a live landmine — the check was structural, so any new SELECT-backed
scope that set `object_type` was silently redirected into fetching `SHOW
TABLES` instead of its own statement, and six scope constructors carried a
defensive comment about it. A new scope no longer has to know.

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
schema. One `SHOW <type> IN DATABASE` returns every schema's objects at once —
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
> comes back at or over `PRIME_CHUNK_THRESHOLD`, falls back to fetching schema
> by schema. Within a *single* schema, where names are unique, keyset paging is
> verified correct and is used — for the object types that honour it.

That threshold used to be read as "Snowflake truncated us at 10 000". It is
read as "this response is too big to take in one bite" now, which reaches the
same decision for an honest reason: a `SHOW` result at the threshold may or
may not be complete — `SHOW COLUMNS` has returned 11 511 rows against a
limit of 10 000 — and either way the answer is the same: fall back to a chunk
small enough to trust whole.

### Column cache priming

A table's columns are fetched lazily by default — one `SHOW COLUMNS IN TABLE`
per table, only when that table's detail view is opened (see "The Object
tree" and the registry section below). That leaves most tables in a database
with no cached columns at all, which is fine for the detail pane but means
column names would be absent from search for anything nobody has personally
opened — moot for the moment anyway, since column search itself is currently
disabled entirely (see "Known gaps"); this priming mechanism stays useful for
the detail pane and is ready to serve search again once that is re-enabled.

**Pre-warm cache** (Connection menu) fixes that on demand — there is no
automatic trigger, since there was no good signal found for "the user wants
this now" short of asking. Selecting it fires three background requests at
once for whatever database the cursor is currently under (multiple databases
can be selected, so this acts on the focused one, the same convention
`action_refresh`/F5 uses): the table and view object lists (via the
existing `DATABASE_PRIME` mechanism above) and a `COLUMNS_PRIME` scope. The
first two exist so the third has schema and table names to fall back to, not
as an afterthought — see below.

`COLUMNS_PRIME` mirrors `DATABASE_PRIME` one level deeper, in three tiers:

1. **Whole database, one round trip**: `SHOW COLUMNS IN DATABASE`, judged
   against the same `PRIME_CHUNK_THRESHOLD` as `DATABASE_PRIME`.
2. **Over the threshold → one round trip per schema**: `SHOW COLUMNS IN
   SCHEMA`. This is the common case in practice, since columns multiply table
   count — a database that fit under the threshold for `DATABASE_PRIME` can
   easily blow it here.
3. **A single schema still over it → one round trip per table**: the
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
per-schema-still-too-big fallback is left depending on cache being warm by
then, an accepted gap: it only matters for one schema large enough to
overflow the cap by itself, and the menu action's own object-list priming is
already running concurrently by the time it could matter.

`SHOW COLUMNS IN DATABASE`/`IN SCHEMA` were verified live on 2026-09-09
(Snowflake 10.32.102) against a database with 614 tables/11 511 columns
across 25 schemas: the result shape matches `map_show_columns`'s assumptions
exactly (`TEXT`/`FIXED`/`TIMESTAMP_NTZ`/`VARIANT` blobs all parsed as
expected). Two things did **not** match the assumptions this tiering was built
on: `LIMIT`/`FROM` is a no-op for this command at both scopes, and the same
call returned 11 511 rows, over the 10k figure `MAX_PAGE_SIZE` was once
documented as a hard ceiling.

Neither breaks the tiering. Each tier issues exactly one call and reads
`usable = len(rows) < threshold`, never a `FROM`-based continuation, so a tier
that comes back at or over the threshold simply falls through to the next one.
Columns are in fact the one place the no-op finding costs nothing: the tiers
*are* the chunking mechanism here, and paging was never used. The builders no
longer accept `after`/`limit` at all, so that cannot quietly be reintroduced.

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
order as an unpaged fetch — for databases, schemas, tables and (added
2026-09-09, against a schema with 63 views) views, at both `IN SCHEMA` and
`IN DATABASE` scope. It is **unverified for tasks** — no account probed so
far has had enough of them to page through.

> **`LIMIT`/`FROM` is a confirmed no-op for `SHOW PIPES`, `SHOW COLUMNS` and
> `SHOW STAGES`, at both `IN SCHEMA` and `IN DATABASE` scope.** First
> observed 2026-09-09 on the sandbox account (Snowflake 10.32.102): `SHOW PIPES IN SCHEMA <schema-with-27-pipes>
> LIMIT 5` returned all 27 rows; `SHOW PIPES IN DATABASE
> <db-with-614-pipes> LIMIT 300` returned all 614; the identical query
> repeated with `FROM '<last pipe name>'` returned the same 614 again, not
> the next page. `SHOW COLUMNS IN SCHEMA`/`IN DATABASE` behaved identically.
> Tables and views, tested the same way in the same session, honoured
> `LIMIT` correctly at both scopes — this is specific to a subset of object
> types, not a general property of `IN SCHEMA`/`IN DATABASE`.
>
> Reconfirmed the same day on a second, independent (production) account,
> against schemas at real scale: `SHOW PIPES IN SCHEMA ... LIMIT 10`
> returned all 395 pipes in the schema; `SHOW COLUMNS IN SCHEMA ... LIMIT
> 10` returned all 6655 columns; `SHOW STAGES IN SCHEMA ... LIMIT 10` also
> returned every stage in its schema, unpaged. Confirmed across two
> unrelated accounts, not an artifact of the sandbox's data.

`ObjectType.pageable` is where that finding lives in the code, and the
statement builders consult it: `show_objects`/`show_objects_in_database` emit
no paging tail at all for an unpageable type, and `show_columns_in_database`/
`show_columns_in_schema` no longer take `after`/`limit` arguments. A clause
Snowflake ignores is worse than no clause, because the SQL log then shows a
bound that was never applied.

`jobs.fetch_page` returns no cursor for an unpageable type, so one call is the
whole result and no continuation is ever queued. That also means **truncation
is undetectable for those statements** — without a working `LIMIT` there is no
row count that can mean "there was more" — which is why the row ceiling below
is their only bound.

Snowflake's own "SHOW caps at 10k rows regardless of `LIMIT`" does not hold
universally either: a real `SHOW COLUMNS IN DATABASE` call against the
614-table database above returned 11 511 rows in one response. `MAX_PAGE_SIZE`
is therefore documented as the largest `LIMIT` worth *asking* for, never as a
ceiling anything may rely on.

#### The hang this used to be

Worth keeping on record, because the shape of the bug is the reason for the
guards below rather than for a one-line fix.

`FetchService._execute` decided "there is another page" from
`len(rows) == limit` and resumed with `FROM '<last row's name>'`. Against a
statement that ignores both clauses, every "next page" came back as the
identical full result with the identical last name: **an unbounded loop, not
merely a slow one.** The pass never reached `finish_pass`, and a worker thread
plus a pool connection stayed pinned re-issuing the same query. The default
pool is three workers, so three such scopes wedge the whole app.

Two paths reached it, and the second is the one that matters:

- `_fan_out`, after a whole-database prime came back over the chunk threshold,
  queues one per-schema `OBJECTS` job. It is object-type agnostic, so **pipes
  and stages sit on it identically** — stages were never a separate or lesser
  exposure, which an earlier draft of this document guessed they might be.
- **The staleness sweep** (`MainScreen._sweep`) requests
  `tree.visible_scopes()` *directly*, so it never passes through
  `on_scope_needed`'s `OBJECTS` → `DATABASE_PRIME` upgrade. An expanded Pipes
  or Stages node in a schema with more objects than `PAGE_SIZE` was enough on
  its own, with no truncated prime needed first. Anything that bypasses that
  upgrade is on the paged path, and the sweep is not the only caller that
  could.

Neither probed account had a schema over 1000 pipes (395 on production, 27 on
the sandbox), so this was never observed firing in the wild — the no-op paging
is what was directly observed, and the hang is what the continuation code,
read against it, implied. Lowering `PAGE_SIZE` below the pipe count
reproduces it on a 395-pipe schema, which is also how the fix is verified.

### Bounding a result

Three layers, deliberately independent, because each covers what the others
cannot. Every one of them is a `Setting`, so someone who knows their account
can move it.

1. **Declare what cannot be paged** (`ObjectType.pageable`, above). Fixes what
   is known. Cannot cover a statement nobody has tested yet.
2. **Guard the continuation chain** (`FetchService._execute`, see "The fetch
   scheduler"). Catches an unpageable statement the builder did not know
   about — tasks, most obviously, still unverified.
3. **A row ceiling in the gateway** (`ROW_CEILING`, `PoolGateway._collect`).
   The only bound that applies to every statement rather than to the ones
   somebody remembered to cap. It reads the cursor in batches and raises
   `ResultTooLargeError` past the ceiling, which routes through the ordinary
   worker boundary to `fail_pass`: no sweep, yesterday's rows stay on screen,
   the error is visible. Be clear about what it buys — it bounds what snowcone
   *materialises* and hands the connection back early. It does not stop
   Snowflake from having produced the result. `PoolGateway._fetch`'s blind
   one-shot retry re-raises it explicitly rather than repeating a statement
   that will return just as much the second time.

On top of those, the **history statements carry their own row cap**
(`HISTORY_MAX_ROWS`), because they are the only queries whose size follows how
*busy* an object is rather than how many objects exist. `pipe_activity` and
`task_activity` are `QUALIFY`-reduced to one row per object, but a *single*
pipe's `pipe_activity_history` was bounded by nothing except its 90-day fetch
window — and a production schema showed 433 892 raw `COPY_HISTORY` rows across
190 pipes in 13 days. `ORDER BY <time> DESC LIMIT n` is the fix, and the
`ORDER BY` is half of it: without it a `LIMIT` keeps an arbitrary slice, which
is worse than no cap because it still looks complete.
`INFORMATION_SCHEMA.TASK_HISTORY`'s `RESULT_LIMIT` was exactly that mistake
— pinned at 10 000 with no documented ordering. It is still set, clamped to
the function's maximum, but the outer `ORDER BY ... LIMIT` is what decides
which rows survive.

`pipe_activity`/`task_activity` are deliberately left uncapped: a cap there
could only drop real pipes out of the "Pipes (n)" list, and layer 3 already
covers the pathological case.

> **Counts first was considered and rejected.** For a `SHOW` a count needs
> `RESULT_SCAN(LAST_QUERY_ID())`, an ordinary `SELECT` — so it needs a
> warehouse, breaking the rule the whole tree rests on, *and* the `SHOW` has
> already returned everything by the time it could run. For the history
> `SELECT`s a count costs the same scan as the query and doubles the round
> trips, in a design where round trips dominate. A bounded query gets the rows
> **and** the "was there more" signal in one trip (`len(rows) >= limit`) — the
> same trick `fetch_database`'s `usable` check already used.

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
  progress reported as `Connecting (2/5) ...` / `(wait for browser / MFA: nn
  seconds)` — the same two lines whether it is the lone up-front connection
  or growth confirmed afterwards, and always counted against the pool's
  eventual target size rather than however many connections that particular
  step happens to be opening. The countdown starts at
  `CONNECT_TIMEOUT_SECONDS` (`gateway/pool.py`, 20s), the same value passed to
  the driver as `login_timeout`/`external_browser_timeout` — so a denied or
  abandoned OAuth or password+MFA approval resolves in a short, visible wait
  instead of the driver's undocumented multi-minute default. There is
  deliberately no Cancel button: `snowflake.connector.connect()` cannot be
  interrupted from the outside once called, so a Cancel button could only
  hide the dialog while the attempt kept running unseen underneath — see
  "Accepted design gaps" for the one authenticator this timeout does not
  actually bound.
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
- **"Does not exist" is the exception to that rule**: the connection that
  reported it is healthy, so `PoolGateway._fetch_once` hands it back to the
  pool and only then raises, and `_fetch` does not retry it. Before this,
  every visit to an object dropped elsewhere closed two working connections
  and opened two new ones, each a possible MFA prompt. Kept to that one error
  number on purpose: an expired session is the same driver exception type,
  and that one must still be discarded.

`ConnectionPool.close()` -- called on quit and at the start of a reconnect,
right after `FetchService.stop()` -- only closes connections that are
currently idle. One a worker thread still has checked out is left alone: a
worker's `cursor.execute()` call cannot be interrupted (see "The fetch
scheduler"), so closing that connection from another thread races the
driver's own session teardown against whatever request is still in flight on
it, and was observed to hang the whole app on quit until the query finally
gave up. The straggling connection is simply abandoned instead -- the process
exiting (or, on reconnect, garbage collection once nothing references it any
more) reclaims it for free, and nothing here waits on it.

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
authenticator, role, warehouse and an optional display name, plus the databases last selected for that
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

The two writers own disjoint fields of the same record -- the connect screen
the connection fields and the name, the picker the databases -- so both go
through `ProfileStore.remember`, which merges into the stored record for that
identity instead of replacing it. Rebuilding the record from the connection
parameters alone once wiped a profile's name every time its databases were
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

One clock, not two: `CACHE_TTL` is both the sweep's interval and the footer
countdown's length. There is no separate poll constant. When the countdown
reaches zero, `MainScreen._sweep` asks which of the scopes **currently on
screen** have exceeded `CACHE_TTL` and requests those at background priority,
then the countdown restarts at `CACHE_TTL`. Scopes nobody is looking at are
never refreshed this way; anything urgent was already requested
interactively. A TTL of `0` disables the sweep entirely.

Navigating to something that changes what's on screen — a different
database, schema, category or object — restarts the countdown immediately
too (`MainScreen._restart_countdown`), rather than leaving the footer
implying a longer wait than what's actually left. This does not also trigger
`_sweep()` on the spot: the view just shown already asked interactively for
anything of its own that was stale, on the way in (`_ScopedView.ensure`,
called from `update_content`) — restarting the countdown only stops it from
*over-promising*, since the freshness of what's newly on screen is already
handled. Nothing fetches in the background between those two triggers
(countdown reaching zero, or a navigation that changes what's shown) except
the initial requests `MainScreen.on_mount` issues at startup.

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
replaced.

**A view owns its whole body.** There used to be a second registry contract —
a view's `compose_context()` fed a separate "Context bar" pane above the
Detail pane — but that pane was never a distinct concept, only
selection-dependent content cramped into a one-line strip by its own
container. The two are one pane now, and `compose_context()` is gone with it.

**A view declares which cache scopes it reads** (`scopes()`). That is how it
knows whether an update concerns it, and how its data joins the staleness
sweep.

### Four families, two registries

| Node under the cursor | View family | Found by |
|---|---|---|
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
  `ViewListView`, `StageListView`, `TaskListView`, `TableView`.
- **Tabs** when there are two genuinely different shapes for one node, with
  mnemonic switching (hidden from the footer, the pattern sibling project
  snakecam's `JobDetailsScreen` established): `PipeView` and `TaskView`
  (history + definition), `ViewView` (columns + definition), `PipeListView`
  (status + history), `SchemaView`/`DatabaseView` (grants + tags).
- **A detail panel under a table**, for what does not fit in a cell: the
  history views' error message for the highlighted row, or (`ColumnsView`)
  a column's tags.

Two of those decisions are worth their reasoning:

`PipeListView` is tabbed but `TaskListView` is not, even though a pipe and a
task look symmetrical. Status ("which of my pipes are broken?") needs *every*
pipe in the schema to be useful, so it lives on the list rather than on a
single pipe's view — and it costs no new scope, since `invalid_reason` is
already on the `SHOW PIPES` rows the list reads anyway. A schema's tasks have
only the one thing to show today, so a single table is enough.

`ViewView` is tabbed because a view is the first type that is genuinely
**both** shapes the registry already had: a real column list, like a table
(`ColumnsView` from `table_view.py`, reused directly rather than duplicated),
*and* one defining SQL statement, like a pipe or task (`show_views.text`,
read through `Repository.object_detail` with no scope of its own — the same
way `show_pipes.definition` and `show_tasks.definition` are).

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
piece is there because the duplicated version had already drifted:

- **`HoverTooltipTable`** — shows a cell's full-precision timestamp on
  hover, for the five list views whose columns are too narrow for one. It
  maps a **column header** to the field holding the untruncated value; the
  five copies it replaced each mapped a *column index*, so reordering a
  view's columns would have silently aimed its tooltips at the wrong one.
- **`TabbedView`** — the mnemonic tab switch, in one place instead of five.
- **`ItemDetailView`** — the panel below a table describing whichever row is
  highlighted, hidden when that row has nothing to say. Two consumers:
  `HistoryView` (below), for `PipeView`/`TaskView`'s load/run errors, and
  `ColumnsView` (`table_view.py`), for a column's tags. Only `detail_text`
  and which table to watch differ per view.
- **`HistoryView`** — adds the two-source history table and its
  capped/empty/errored note on top of `ItemDetailView`, shared by `PipeView`
  and `TaskView`.

The widget ids are shared too (`detail-list`, `detail-history`, `detail-item`,
`detail-definition`, ...), and deliberately: exactly one view is mounted at a
time, so `#detail-history` means "whichever history table is on screen", and
`#detail-item` means "whichever row's detail is on screen" whether that's an
error or a column's tags. The per-view ids these replaced had already cost
something real — `app.tcss` styled the pipe view's history table, detail
panel and definition pane, and the task view, a later copy of all three under
its own ids, silently picked up none of it.

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
and not a term snowcone made up. Type and Tags have no tooltip, because no
single Snowflake name would be true of them. Type is `DATA_TYPE` recombined
with its length, precision and scale. `DATA_TYPE_ALIAS` was rejected: it is
the alias written in the DDL, such as `INT`, which SHOW COLUMNS does not
return and the cache does not hold. Tags is a count of the
`TAG_REFERENCES_ALL_COLUMNS` rows. A test holds every tooltip to a column the
cache actually has.

Type and Tags are computed, but only restate what the cache holds. There is
exactly one *inferred* column in the app, one that stands in for a Snowflake
value snowcone never reads, and it is called out as such in
`pipe_list_view.py`: the Pipes (n) Status tab's **Execution State**, which
is `RUNNING` when `invalid_reason` is empty and `STOPPED` when it is not.
Snowflake's own name for the concept is `SYSTEM$PIPE_STATUS`' `executionState`,
which snowcone never queries — it is a per-pipe function call, so covering a
schema with it would cost one round trip per pipe. The column earns its place
because an empty `invalid_reason` cell alone reads as "nothing fetched"
rather than "this pipe is fine", but only two of `executionState`'s values
are reachable this way.


---

## Search

A modal (`SearchDialog`), opened by `/` at roughly 90 % of the screen and
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
in `SearchDialog.on_mount` is commented out.

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
added purely at render time (`SearchDialog._option_text`) and deliberately
excluded from ranking itself. Extending the schema/name qualification to a
third, database level would spend the query's leading characters on the
database too, for no benefit: there is no database-repeats-inside-object-name
ambiguity the way `STAGING`/`STG_` justifies it for schema. Don't assume the
database participates in match scoring the way the schema does — it doesn't.
It can still be *matched*, by a filter word (below), just never scored.

**Multi-word queries: the first word ranks, the rest filter.** Everything
above describes the query's first word; it is matched, scored and ordered
exactly as a one-word query always was. Each further word, after a space, only
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
somewhere you can keep browsing from. The object is scrolled to the middle
of the tree rather than the least distance into view, which would pin a hit
further down to the last visible row. A **schema** hit is different —
`SchemaRef` carries only `database`/`name`, deliberately not an `ObjectRef`
with some placeholder type, since a schema has no `ObjectType` and no mirror
row of its own to show. Choosing one instead **reveals and expands it** in
the tree (`ObjectTree.reveal_schema`), which moves the cursor onto the
schema node itself — and since a schema node now announces itself on
highlight (`SchemaSelected`, see "Detail views" below), that
also opens its Grants/Tags view, not just the tree navigation alone.

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
  legitimately does. Picked from **Connection > Role ...** / **Warehouse
  ...**, the same fuzzy-picker-over-cached-rows pattern as the database
  picker (`PickerDialog`, single-select -- the database picker is the
  multi-select sibling, `MultiPickerDialog`, see "The Object tree" above),
  backed by their own cache scopes (`SHOW WAREHOUSES`, `SHOW ROLES`
  — small, flat, unpaged lists, unlike the object mirrors), requested
  eagerly on mount alongside the database list rather than on the picker's
  first open — lazily fetching it there meant the first open ever just
  requested the list and closed again, since there was nothing cached yet
  to show. Unlike the
  database picker, which only ever changes what a query is built to ask for,
  picking a warehouse or role actually rewrites the session: see
  `ConnectionPool.set_role` / `set_warehouse` below.
- **Countdown** — seconds until the next background staleness check (see "The
  staleness sweep" above). A global timer, deliberately: `CACHE_TTL` is both
  its length and the sweep's own interval, so this is one clock, not a
  per-scope freshness readout. Navigating to something new restarts it. It
  becomes a spinner while anything on screen is being refetched, and blanks
  when the TTL is `0`. Fixed width, right-aligned, no leading zeroes, so the
  digits do not shift as it
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

## The SQL log

`F1` slides a log panel down over the top third of the screen; `F1` again
hides it. It is `tcoda`'s `LogViewer` (see "Shared widgets"), mounted in
`MainScreen.compose` with its defaults, so snowcone wires nothing for it: the
widget installs its own key binding and its own screen layer, owns a rolling
in-memory buffer that exists whether or not the panel is mounted, and offers
level and source filters of its own.

What snowcone contributes is the content. **`gateway/sql_log.py` logs every
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
`INFORMATION_SCHEMA`/`ACCOUNT_USAGE` `SELECT`, or a bare `USE ROLE`/`USE
WAREHOUSE`. Reaching for that masking would also mean importing
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
without a restart.

All of that machinery -- `Setting`, the registry, `ConfigStore`, the dialog --
is `tcoda`'s (see "Shared widgets" below). `snowcone/settings.py` is only the
declarations plus the one `configure("snowcone")` call that tells the shared
store which app's config file to use; the store resolves that path lazily, on
first read, so importing the module touches no disk.

The ones that matter: **cache TTL** (`0` disables background refreshing),
**pool size**, **fetch page size**.

Four more exist purely so the measures in "Bounding a result" are adjustable
rather than baked in: **history row cap** (`HISTORY_MAX_ROWS`), **max pages
per refresh** (`MAX_PAGES_PER_PASS`), **result row ceiling** (`ROW_CEILING`,
`0` disables) and **prime chunk threshold** (`PRIME_CHUNK_THRESHOLD`). Their
defaults are starting points, not measured optima — an account whose shape
makes one of them wrong should be able to say so without a code change.

`PAGE_SIZE` is the one setting **not** read at the point of use: it is
snapshotted into `FetchService` at construction, so a change only takes effect
on the next `restart_fetch` (a reconnect or a role switch). Not deliberate,
just unfixed — and worth knowing when lowering it to reproduce a paging
problem.

**Settings ...** (Snowcone menu) opens a modal listing every registered
`Setting`, grouped under a heading per `Setting.group`, each editable in
place. **OK** validates and writes, then closes; **Apply** does the same but
leaves the dialog open; **Cancel**/escape discard, confirming first if
anything was typed that differs from what was last saved -- the same "unsaved
changes" guard `ConnectScreen` uses. Clearing a field's text and confirming is
how a setting is reset to its default. A value that fails to parse
(`ValueError`) blocks OK/Apply with an inline warning rather than silently
discarding it or falling back to the default -- that fallback exists for a
hand-edited settings.json, not for a mistake made in the dialog itself.

`tcoda`'s dialog also supports per-instance settings and callback-only action
rows, which snowcone declares none of; they exist for the sibling app the
dialog originally came from.

---

## Shared widgets

The menu bar, the settings system, the log panel (`LogViewer`, see "The SQL
log") and the dialog chrome underneath the dialogs (`Dialog`, `ConfirmDialog`)
live in **`tcoda`** ("Textual coda"), a sibling project shared with
`snakecam` and `vc`. They were extracted because the same code had been
copied into three apps and had already started to drift.

It is consumed as an editable path dependency (`[tool.uv.sources]` in
`pyproject.toml`), so an edit there is live here with no reinstall.

> **`tcoda` must never appear in `[project.dependencies]`.** It is not published
> anywhere, so a built snowcone wheel instead bundles a copy of its sources
> directly (`hatch_build.py`, triggered as a custom build hook on both the
> `sdist` and `wheel` targets — see the hook's own comments for how it locates
> a source copy for each). `[tool.uv.sources]`'s path override only takes
> effect inside this project's own `uv sync`/lock resolution; it does nothing
> for a plain `pip install`/`uv pip install` of the built wheel elsewhere. If
> `tcoda` were listed as a real dependency, any such install would resolve it
> against PyPI instead — where it either fails to resolve or, should anyone
> ever claim the name, silently shadows the bundled copy with the wrong
> package. `tcoda` is
> therefore listed only under `[dependency-groups] dev`, which `uv sync`
> honors for the local venv but which never becomes part of a built
> distribution's metadata.

Three things about it are worth knowing from inside snowcone:

- **`tcoda` ships its TCSS inline**, as `DEFAULT_CSS` on each widget, rather
  than in a stylesheet -- a library cannot rely on a consumer's `CSS_PATH`.
  So the menu-bar and settings-dialog rules are no longer in `app.tcss`.
  `app.tcss` outranks `DEFAULT_CSS` regardless of specificity, so anything
  tcoda does style can still be overridden here.
- **`MenuBar` installs its own `F9` binding** on whichever screen it is
  mounted on, which is why `MainScreen.BINDINGS` no longer carries one.
  Textual only consults the focus chain plus the screen and the app when
  resolving a key, so a bar that is never focused cannot own the binding
  itself.
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

---

## Tooling and conventions

- **uv** manages the project and the virtualenv. Use `uv sync --all-groups` for
  dependencies and `uv add` to introduce one. The interpreter is the project
  `.venv`. `tcoda` is an editable checkout of a sibling directory rather than a
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

### Unverified against a live account

Every `SHOW`-sourced mirror in `schema.sql` was verified against a live
account (see that file's header). Every `INFORMATION_SCHEMA`/`ACCOUNT_USAGE`
mirror's **column names** were originally taken from Snowflake's published
documentation only, unconfirmed against a real account — a wrong name would
surface as that one scope rendering blank, never a crash, per the
degrade-blank convention. On 2026-09-09 (Snowflake 10.32.102,
a sandbox account whose `RENETEST1`/`2`/`3`
databases carry 614-657 tables and pipes each, apparently built for exactly
this kind of scale check) every one of those column lists was run for real
and resolved without error:
`information_schema_tables`, `information_schema_views`,
`account_usage_copy_history`, `account_usage_copy_history_detail`,
`information_schema_copy_history`, `account_usage_task_history`,
`account_usage_task_history_detail`, `information_schema_task_history`,
`information_schema_tag_references` and `show_grants` are all confirmed —
none of these column sets is a documentation guess any more.

What that first pass could **not** confirm was *values*, for scopes where
the sandbox account simply had no matching data. A second round on
2026-09-09, partly against a real production account (queries handed to the
user to run and relay counts back from, since neither of us wanted to put
this session's hands directly on production) and partly by deliberately
creating test data on the sandbox (a materialized view, and a real task —
both left in place), closed all of it except the `ACCOUNT_USAGE` lag figure,
which turned out not to be worth chasing — see the last bullet below:

- `information_schema_tables`/`information_schema_views`: `table_stats`
  (`TABLE_TYPE = 'BASE TABLE'`) returned real `LAST_DDL`/`LAST_ALTERED`
  timestamps for 27 real tables on the sandbox — genuinely verified, not
  just column-resolved. `view_stats` is the identical statement bound to
  `'VIEW'` instead; the sandbox held no user-created view, but production
  did — 37 real views, all 37 with non-null `LAST_DDL` and `LAST_ALTERED`.
  **Fully confirmed.**
- `show_materialized_views`: column names were already confirmed; values
  were not, since neither probed account had a materialized view. Closed by
  creating one on the sandbox (500 inserted rows into `RENETEST1.
  ABIDING_NOUGAT.ADAMANT_RELISH`, then `CREATE MATERIALIZED VIEW ... AS
  SELECT ... FROM` that table): the resulting `SHOW MATERIALIZED VIEWS` row
  carries `rows = 500` and `bytes = 11264` — both real, non-null, and `rows`
  matches the insert exactly — plus a correctly resolved
  `source_database_name`/`source_schema_name`/`source_table_name` and
  populated `refreshed_on`/`compacted_on`. **Fully confirmed.** Left in
  place on the sandbox at the user's request, not cleaned up.
- `account_usage_copy_history`(`_detail`)/`information_schema_copy_history`:
  **confirmed**, including the `QUALIFY` reduction's actual row-collapsing
  behaviour, not just its SQL validity. A raw, unreduced
  `ACCOUNT_USAGE.COPY_HISTORY` count against one production schema's pipes
  (13-day window) came back at **433 892 rows** — real evidence the scale
  this reduction guards against is live and current on that account, not
  just a historical incident. The same window run through the actual
  `PIPE_ACTIVITY` reduction came back at **190 rows, 190 distinct pipe
  names** — a 1:1 match, and a ~2284x collapse from the raw count. The
  `QUALIFY ROW_NUMBER() OVER (PARTITION BY PIPE_NAME ORDER BY
  LAST_LOAD_TIME DESC) = 1` reduction is no longer confirmed only by the
  hang it originally fixed on a different, unreduced query — it now has a
  direct, real-data confirmation of its own.
- `account_usage_task_history`(`_detail`): **confirmed** on production — 7
  real task runs, all from 1 distinct task, over a 7-day window.
  `information_schema_task_history` is **confirmed** too, by creating a real
  task on the sandbox (`RENETEST1.ABIDING_NOUGAT.CLAUDE_VERIFY_TASK`, `AS
  SELECT 1`, three manually triggered runs): `SCHEDULED_TIME`,
  `COMPLETED_TIME`, `STATE` and, on the runs that failed (the sandbox's
  `SYSADMIN` role turned out to lack `USAGE` on warehouse `WH` — an account
  setup gap, not a snowcone one), `ERROR_CODE`/`ERROR_MESSAGE` all came back
  real and non-null. `QUERY_ID`/`QUERY_START_TIME` stayed `NULL` on every
  run, but that's consistent with the task failing before a query was ever
  submitted, not a sign the columns don't populate on success — not chased
  further, since a `USAGE` grant on that warehouse would need `ACCOUNTADMIN`
  to fix, which is out of scope for a verification pass.
- `information_schema_tag_references`: **confirmed** on production — 3
  real tag references, covering 3 distinct tag names.
- `information_schema_tag_references_all_columns` (`COLUMN_TAG_REFERENCES`,
  added after this pass) was checked against a real account on 2026-09-14
  and found wrong: `TAG_REFERENCES_ALL_COLUMNS` reports a schema- or
  database-level tag against every column of every table underneath it too
  (`APPLY_METHOD = 'INHERITED'`), so every column showed its schema's tags
  as if they were its own. Fixed by adding `WHERE APPLY_METHOD IS DISTINCT
  FROM 'INHERITED'` to the query (`queries.tag_references_all_columns`).
  **Now confirmed**, including the fix.
- `granted_by_role_type` on `SHOW GRANTS` is **no longer speculative**: it
  exists and reads `'ROLE'` on real `SHOW GRANTS ON SCHEMA`/`ON DATABASE`
  output.
- `information_schema_copy_history`'s `PIPE_NAME` output column being
  bare/unqualified, and its `TABLE_NAME` argument accepting a
  fully-qualified `catalog.schema.table` string, are **both confirmed** —
  the statement referencing both compiled and ran with no "invalid
  identifier" error.
- Whether `INFORMATION_SCHEMA.TASK_HISTORY` needs a running warehouse is
  **confirmed yes** (tested directly: `No active warehouse selected in the
  current session` with none active) — no longer just an analogy with
  `COPY_HISTORY`/`TAG_REFERENCES`, which were tested the same way and also
  confirmed to need one, along with `table_stats`/`view_stats`.
- `ACCOUNT_USAGE` lag figures (~2 h for copy history, ~45 min for task
  history) are Snowflake's published numbers and **not something this
  project tracks further by design decision**: the lag is neither
  guaranteed nor reproducible, so no measurement taken at any one moment
  would be more trustworthy than Snowflake's own "up to ~X" figure, and
  chasing a tighter number would just be a snapshot masquerading as a
  guarantee. (A production comparison during this verification pass did
  show `INFORMATION_SCHEMA.COPY_HISTORY` seeing one load 2 minutes 28
  seconds before `ACCOUNT_USAGE.COPY_HISTORY` did — consistent with the
  published figures, not a tighter bound on them, and not chased further
  for the reason above.)

Paging carries a related but different caveat now — see "Verified and
unverified paging" for what was confirmed (and what was found actively
broken, on two independent accounts: `LIMIT`/`FROM` is a no-op for `SHOW
PIPES`, `SHOW COLUMNS` and `SHOW STAGES`). That finding is no longer only
recorded: it is enforced in the statement builders, and guarded against for
the types nobody has probed yet — see "Bounding a result". `SHOW COLUMNS IN
DATABASE`/`IN SCHEMA` were exercised at real scale (614 tables, 11 511 columns
on the sandbox; 6655 columns in one schema alone on production) — see "Column
cache priming".

**`SHOW TASKS` is still unverified for `LIMIT`/`FROM`**: no probed account has
had enough tasks to page through one. It is no longer a hang risk either way
— if it does ignore both clauses, the cursor-progress guard completes the pass
after a single repeat instead of looping — but verifying it would let
`ObjectType.pageable` state a fact rather than a default.

### Accepted design gaps

- The Pipes (n) **Execution State** column is derived from `invalid_reason`,
  not read from `SYSTEM$PIPE_STATUS` — see "Column headers mirror Snowflake".
  Only `RUNNING` and `STOPPED` are reachable; Snowflake's `PAUSED` and
  `STOPPED_*` variants are not distinguishable this way.
- The tree/Detail drag-resize does not persist across sessions, and does not
  re-clamp an already-narrow pane if the terminal is then resized smaller --
  both ported as-is from sibling project snakecam's identical mechanism.
- A single stage's own leaf node has no Object view. Deliberate, not an open
  gap: see "What decides a view's shape".
- **Multiple databases can be browsed and searched at once**, reversing what
  used to be a deliberate constraint here. The concern that constraint was
  protecting against — total object counts across many databases getting
  impractical — is not addressed by anything new; it simply hasn't been hit
  at the scales exercised so far. Treat performance at a large multi-database
  selection as an open question.
- Databases still aren't themselves a *searchable hit* the way a schema is:
  selecting one is a picker action, not a search result.
- Column search is implemented end to end at the repository layer
  (`Repository.search_column_corpus`) but its one call site in
  `SearchDialog.on_mount` is commented out — cross-database column search was
  judged not ready. Re-enabling it is restoring one call, not writing code.
- There is no automatic trigger for column cache priming — no good signal was
  found for "the user wants this now" short of asking, so it is a manual menu
  action.
- `CONNECT_TIMEOUT_SECONDS` (`gateway/pool.py`) bounds a denied or abandoned
  approval for OAuth and password+MFA, but not for browser SSO
  (`externalbrowser`): the installed driver's `AuthByWebBrowser` waits on a
  raw `select.select()` with no timeout argument anywhere in that code path,
  confirmed by reading it rather than assumed — no kwarg reaches it, and
  `socket.setdefaulttimeout()` would not either, since `select.select()`
  ignores a socket's `.settimeout()`. An abandoned browser tab under that
  authenticator can still leave the "Connecting ..." dialog counting down to
  0 and sitting there indefinitely. A client-side watchdog was considered and
  rejected for now: the driver gives no way to reclaim the blocked thread or
  its open listening socket, so a watchdog could only abandon the dialog, not
  the resource underneath it.

### The cost of adding a scope

Adding one scope currently touches ten places, and there is no shortcut:

1. a `ScopeType` member, 2. a `*_scope()` constructor, 3. a case in
`Scope.key`, 4. a `TableSpec`, 5. a case in `Repository.spec_for`, 6. a table
in `schema.sql`, 7. a builder in `gateway/queries.py`, 8. a method on the
`SnowflakeGateway` protocol, 9. the same on `PoolGateway`, 10. a case in
`jobs.fetch_page`.

That grew from six scopes to twenty without anyone deciding it should. A
table-driven scope registry would collapse most of it, but it is a large
change with no bug behind it, so the list above is the checklist until it
starts hurting.

