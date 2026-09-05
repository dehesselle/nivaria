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
│ ┌─ MYDB ──────────┐│ Search bar                                          │
│ │                 │├ ....................................................│
│ │                 ││ Context bar                                         │
│ │                 ││  (replaced by Search results while searching)       │
│ │   Object tree   │├─────────────────────────────────────────────────────┤
│ │                 ││                                                     │
│ │                 ││              Detail pane                            │
│ │                 ││        (hosts exactly one Object view)              │
│ └─────────────────┘│                                                     │
├────────────────────┴─────────────────────────────────────────────────────┤
│ ^d Database  / Search  r Refresh  q Quit  Warehouse  Role  Countdown  (2/5)│
└──────────────────────────── Status footer ───────────────────────────────┘
```

| Name | What it is |
|---|---|
| **Menu bar** | Midnight-Commander style: hidden except while in use. `F9` (or clicking a title) opens it in a bar-only stage that arrows/mnemonics move between titles without showing items yet, then `enter`/mnemonic drops that title's menu; escape backs out a stage at a time. Mnemonics are the theme's accent color rather than underlined. Currently one menu, **Connect** (`c`), with **Open...** (`o`) to reopen the connect dialog and swap in whatever pool it returns, **Warehouse ...** (`w`) and **Role ...** (`r`) to open the same fuzzy-picker pattern as the database picker over cached warehouses/roles, applying the choice session-wide (see below). |
| **Object tree** | The browser. `Schemas (n)` → schema → `Tables (n)` / `Views (n)` / … → object. Its border title is the current database (`(none)` until one is picked); `ctrl+d` opens the **Database picker**, a fuzzy modal over cached databases. |
| **Top pane** | Holds the Search bar plus one of: Context bar, or Search results. |
| **Search bar** | Find-as-you-type over every cached object in the current database. `/` focuses it. |
| **Context bar** | Contributed by the active Object view — identity and headline facts about what is selected. |
| **Search results** | Ranked hits. Takes the Context bar's place while a query is active, because the top pane shows one thing at a time: what you are looking at, or what you are looking for. |
| **Detail pane** | Hosts exactly one **Object view** at a time, chosen by object kind. |
| **Object view** | The per-kind detail UI (e.g. Table view, Pipe view). Owns both its detail body and its Context bar contribution. |
| **Status footer** | Key bindings on the left; **Warehouse**/**Role** labels, **Countdown** and **Pool gauge** in the right corner. Textual's command-palette key is suppressed so the corner is ours. |

The right-hand split is 25 % top pane / 75 % Detail pane; the left pane is 25 %
of the width. All sizing is in `fr` units, never percentages, so panes stay
resizable later without rework.

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
  confirmation dialog spelling out how many *additional* connections that
  means, with plain **OK** / **Cancel** options. Declining leaves the pool at
  size one rather than failing or silently upgrading — which is also what
  keeps a pool size of one always reachable on demand, handy under test and
  for a quick look that would rather not field extra MFA prompts.
- Confirmed growth, like the first connection, is opened **eagerly**, with
  progress reported as `Connecting 2/5 …`.
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

Two authenticators are supported and chosen in the connect dialog: browser SSO
(`externalbrowser`) and password + MFA (`username_password_mfa`). The password
field is disabled for SSO, where the identity provider owns the credentials and
a typed password would be silently discarded.

**snowcone stores no password anywhere.** Recent connections keep account,
user, authenticator, role and warehouse only. For SSO there is nothing to
store; for password+MFA the driver's own token cache already means the password
is typed once per machine rather than once per connection. Connections are
recorded as "recent" only after they have actually worked.

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

**A view owns both its detail body and its Context bar contribution.** They are
one concept — "what you can do with a pipe" — and splitting them across two
registries would guarantee they drift apart.

A view declares which cache scopes it reads. That is how it knows whether an
update concerns it, and how its data joins the staleness sweep.

An object kind with no registered view shows a placeholder rather than
crashing, so the tree can list kinds ahead of their views existing.

The kinds are genuinely different shapes, which is the justification for the
seam: a table shows columns; a pipe has no columns at all, showing its COPY
statement and a prominent banner when it has stopped, because a stopped pipe is
indistinguishable from a healthy one until you read that field.

---

## Search

Find-as-you-type over every cached object in the current database, ranked by
subsequence matching so `stgord` finds `STG_CUSTOMER_ORDERS`. Substring search
would not, which is the whole point; sqlite's full-text matching cannot express
it either, so ranking happens in Python over an in-memory corpus.

The corpus is rebuilt lazily — a cache update marks it stale and the next
keystroke rebuilds it. A database prime lands dozens of scope updates in a
second, and rebuilding on each would be wasted work.

Matching scores **both `SCHEMA.NAME` and the bare name**, keeping whichever is
better, with a small preference for the name. This is not gold-plating:
Snowflake schemas repeat themselves in their objects' names — a `STAGING`
schema full of `STG_` tables — and greedy left-to-right matching otherwise
spends the query's leading characters inside the schema, leaving a poor
alignment in the name. Without this, `stgord` ranks `STAGING.LOAD_ORDERS`
*above* `STAGING.STG_CUSTOMER_ORDERS`.

Choosing a hit **reveals it in the tree** as well as opening it, so search
leaves you somewhere you can keep browsing from.

Ranking runs on the event loop. Measured cost for a whole corpus, worst query:
2 k objects 3 ms, 10 k 14 ms, 50 k 70 ms, 100 k 150 ms. Comfortable at the
sizes this targets, and the input is debounced on top. **Beyond roughly 20 k
objects in one database this wants moving to a thread** — the matcher is a
plain function precisely so that stays an easy change.

---

## Status footer

The right corner carries four indicators. Textual's command-palette binding is
suppressed so the corner is available; the footer is otherwise Textual's, so
key bindings keep working without being re-implemented.

- **Warehouse** / **Role** — the pool's current warehouse and role, read
  straight off the pool's connect params each tick; each reads `none` until
  one has ever been set. Picked from **Connect > Warehouse ...** / **Role
  ...**, the same fuzzy-picker-over-cached-rows pattern as the database
  picker, backed by their own cache scopes (`SHOW WAREHOUSES`, `SHOW ROLES`
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

---

## Settings

Settings are declared once and self-register, so a settings UI can enumerate
them with no per-setting wiring. Only values that differ from their default are
persisted — a setting nobody has touched stays absent from the file, so a
future release can change its default without a stale override shadowing it.
Values are read **at the point of use**, never cached, so a change takes effect
without a restart.

The ones that matter: **cache TTL** (`0` disables background refreshing),
**pool size**, **fetch page size**.

---

## Tooling and conventions

- **uv** manages the project and the virtualenv. Use `uv sync --all-groups` for
  dependencies and `uv add` to introduce one. The interpreter is the project
  `.venv`.
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
- Stages and tasks appear in the tree but have no Object view yet; they show a
  placeholder.
- Schemas and databases are not themselves searchable — only objects within
  them.
- Search covers the current database only, since that is what is cached.
- Cross-database browsing is out of scope by design: the object counts make it
  impractical, which is why the database is a one-line selector rather than a
  level in the tree.
