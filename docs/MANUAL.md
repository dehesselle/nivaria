# nivaria manual

This manual covers using nivaria: connecting, the screen and its keys,
browsing, search, the detail views and every setting.

How nivaria behaves while running — background refreshes, Snowflake load,
limits, files on disk and troubleshooting — is in
[OPERATIONS.md](OPERATIONS.md).

## Running

```sh
uv tool install nivaria
nivaria
```

`nivaria --version` prints the version. There are no other command-line
options; everything else is set in the app.

## Connecting

nivaria opens on the connect dialog: the form on the left, recent connections
on the right.

- **account** and **user** are required. **role** and **warehouse** are
  optional; left blank, Snowflake's defaults for the user apply, and the
  footer shows what the session actually resolved.
- **Authenticator**: OAuth (the default, Snowflake's own authorization-code
  flow in a browser), browser SSO (`externalbrowser`), or password + MFA. The
  password field is enabled only for password + MFA.
- **name** is an optional display name for the recent entry.
- Picking a recent entry fills the form. `Delete` removes the highlighted
  recent entry. `Escape` cancels.

Submitting opens one connection. If the **connection pool size** setting is
more than one, nivaria then asks "Do you want to expand the connection pool to
N?"; **No** keeps a single connection. With OAuth it grows the pool without
asking, because the first login's token covers the others. Each connection
shows `Connecting (n/N) ...` and, for browser or MFA approval, a countdown.

A connection is remembered as recent only after it worked. nivaria stores no
password; see [OPERATIONS.md](OPERATIONS.md#files-on-disk) for what it does
store.

## Screen anatomy

These are the names used for the parts of the screen, here and in the other
documents.

```text
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

`/` opens the **Search modal** over this screen, at roughly 90 % of the
terminal:

```text
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
| --- | --- |
| **Menu bar** | Hidden except while in use, Midnight-Commander style: opened by `F9` or by clicking a title. Two menus, **Nivaria** and **Connection** (see "Menus"). |
| **Object tree** | The browser. One top-level node per selected database, showing its schema count (`MYDB (12)`), expanding into schemas → `Tables (n)` / `Views (n)` / … → object. |
| **Search modal** | Find-as-you-type over every cached object and schema across every selected database. |
| **Detail pane** | Shows one detail view for whatever node the cursor is on. Its border with the Object tree can be dragged to resize both panes. |
| **Object view** | The detail view for one object (e.g. Table view, Pipe view). Category, schema and database nodes have views of their own (see "Detail views"). |
| **Status footer** | Key bindings on the left; **Role**, **Warehouse**, **Countdown** and **Pool gauge** on the right. |
| **Log panel** | Hidden until `F1`, then slides down over the top third of the screen, showing every statement nivaria sends, live. `F1` hides it again. It has its own level and source filters. |

The tree takes a quarter of the width at start. A dragged width is not kept
across sessions.

## Keys

| Key | Action |
| --- | --- |
| `F9` | Open the menu bar |
| `F1` | Show or hide the Log panel |
| `/` | Open search |
| `F5` | Refresh whatever the cursor is on, and what the detail pane shows for it |
| `w` | Open whatever the cursor is on in Snowsight |
| `Ctrl+O` | Reopen the connect dialog (reconnect, or switch account) |
| `Ctrl+D` | Pick databases |
| `Ctrl+W` | Pick warehouse |
| `Ctrl+R` | Pick role |
| `Ctrl+Q` | Quit; press again to quit without waiting for running statements |

## Menus

**Nivaria** (`n`): **About ...** (`a`), **Settings ...** (`s`), **Quit** (`q`).

**Connection** (`c`): **Open...** (`o`), **Databases ...** (`d`),
**Warehouse ...** (`w`), **Role ...** (`r`).

A letter in parentheses selects the entry while its menu is open.

## Browsing

`Ctrl+D` opens the **Databases picker**: type to filter, `space` or a click
toggles a database, and `enter` or **Ok** confirms. Each selected database gets
its own top-level node, and the selection is remembered per connection.

Expanding a node lists its children from the cache at once, and requests a
refresh in the background if the cache is missing or old; the tree updates in
place without moving the cursor. Counts on collapsed nodes come from the cache.

Moving the cursor shows the node in the Detail pane. `enter` on a row of a
category list (for example `Tables (n)`) opens that object and moves the tree
cursor to it.

`w` opens the tree cursor's database, schema or object in Snowsight in the
default browser; on a category such as `Pipes (n)` it opens the schema. The
link uses the organization and account name nivaria reads from Snowflake when
connecting, so any form of account identifier works, and opens the public
`app.snowflake.com`. A PrivateLink account (an identifier ending in
`.privatelink`) reaches Snowsight through a private host that depends on its
network setup, so `w` asks for **Snowsight URL** in Settings instead: enter the
address Snowsight shows for the account up to and including the account name,
for example
`https://app.eu-central-1.privatelink.snowflakecomputing.com/myorg/myacct`.
The setting is kept per account and, when set, is used for any account.

**Warehouse ...** and **Role ...** switch the whole session. A role switch
changes what Snowflake lets you see, so nivaria switches to that role's own
cache; switching back later finds it still filled. See
[OPERATIONS.md](OPERATIONS.md#switching-role-or-warehouse).

## Search

`/` opens search. Matching is by subsequence, so `stgord` finds
`STG_CUSTOMER_ORDERS`. The first word ranks the results by name (and schema);
each further word, after a space, only narrows them and must match the
database, schema or name of a row. `MY_TABLE my_schema my_db` picks one table
out of many with the same name. The database is shown but only matched by
those further words, never by the first.

`down` moves into the results, `enter` opens the highlighted hit and closes
search, `escape` closes it without choosing. Opening an object also reveals
it in the tree; opening a schema expands it there and shows its Grants and
Tags.

Search covers what is cached. Column names are not searched.

## Detail views

Tabbed views switch tabs with a letter key while focus is in the view; a
letter means the same tab everywhere.

| Node | View | Tabs (key) |
| --- | --- | --- |
| Database | Database view | Grants (`g`), Tags (`t`) |
| Schema | Schema view | Grants (`g`), Tags (`t`) |
| `Tables (n)` | Table list | — |
| `Views (n)` | View list | — |
| `Pipes (n)` | Pipe list | Activity (`a`), Status (`s`); opens on Status |
| `Stages (n)` | Stage list | — |
| `Tasks (n)` | Task list | — |
| Table | Table view | Columns (`c`), Data (`d`), Grants (`g`) |
| View | View view | Columns (`c`), Data (`d`), Grants (`g`), SQL (`s`) |
| Pipe | Pipe view | History (`h`), SQL (`s`); opens on History |
| Task | Task view | History (`h`), SQL (`s`); opens on History |

A stage has no view of its own; the stage list shows everything about it.

- **Columns** lists a table's columns. Hovering a header shows the Snowflake
  field it reads. Highlighting a column with tags shows them below the table.
- **Data** shows a preview of the table's rows, in no particular order (see
  the data preview settings below). It needs a warehouse.
- **Grants** has one row per grantee; the privileges marked `+` are held with
  grant option. The individual grants of the highlighted grantee are listed
  below the table. Only grants your role may see are shown.
- **History** merges a pipe's load or a task's run history from two Snowflake
  sources. A failed load or run shows its error below the table. A note above
  says when the history was capped. While the slower, older source is still
  being read the first time, the note says what is known so far, such as "No
  run history in the last 7 days. Still looking...". A pipe's recent loads are read through its target table. When the table is
  not known from a load and the pipe's definition does not name it in full
  (`database.schema.table`), new loads are not checked for automatically; the
  note says so, and `F5` checks now.
- **Status** (Pipe list) shows each pipe's `invalid_reason` and the
  execution state Snowflake reports for it (`RUNNING`, `PAUSED`,
  `STOPPED_...`). The state is read, 50 pipes at a time, only while this tab
  is open; a pipe whose state has not been read yet shows none.

Columns sourced from something other than `SHOW` (Last DDL, pipe and task
activity, tags, history, data) stay blank when the session has no warehouse
or lacks the privilege; see
[OPERATIONS.md](OPERATIONS.md#warehouse-and-privileges). Picking a warehouse
fills them in.

When a refresh fails, the rows already shown stay, and a note above the list,
History or Data table says when they were fetched.

## Status footer

- **Role** / **Warehouse**: the session's current role and warehouse.
  Warehouse can read `none`.
- **Countdown**: seconds until the first thing on screen is due a refresh;
  nothing on screen goes stale before it reaches zero. It jumps as what is on
  screen changes, turns into a spinner while something on screen is being
  refetched, and is blank when the cache age setting is `0`.
- **Pool gauge**: `(in use / total)` connections.

## Settings

**Settings ...** (Nivaria menu) lists every setting by group, each editable in
place. **OK** validates, saves and closes; **Apply** saves and stays open;
**Cancel** or `escape` discards, asking first if anything changed. Clearing a
field and confirming resets it to its default. A change takes effect without a
restart, except **fetch page size**, which applies from the next reconnect or
role switch.

| Setting | Default | Effect |
| --- | --- | --- |
| cache age | 300 | Seconds cached metadata stays fresh before what is on screen is refreshed in the background. `0` disables background refreshing; `F5` still works. |
| connection pool size | 3 | Connections to keep open (1–5). Every connection may cost an MFA prompt. |
| Snowsight URL | empty | Per account: the Snowsight address `w` opens, up to the account name. Empty uses the public address; a PrivateLink account needs it set. |
| fetch page size | 1000 | Rows one fetch job asks for. Lowering it mainly makes fetching slower. |
| pipe history fetch window (days) | 90 | Days of pipe load history fetched and cached. |
| pipe history display window (days) | 90 | Days of it shown in the Pipe view. |
| task history fetch window (days) | 90 | Days of task run history fetched and cached. |
| task history display window (days) | 90 | Days of it shown in the Task view. |
| history row cap | 5000 | Most rows one history statement returns; the newest are kept. |
| data preview rows | 100 | Rows the Data tab fetches. |
| data preview timeout (seconds) | 15 | How long a Data tab query may run before it is cancelled. `0` means no limit. |
| max pages per refresh | 50 | Pages one refresh may fetch before it gives up with an error. |
| result row ceiling | 200000 | Most rows read from any single statement before it is abandoned with an error. `0` disables it. |
| prime chunk threshold | 10000 | Result size above which a whole-database fetch is redone per schema. |

Settings live in `settings.json` (see
[OPERATIONS.md](OPERATIONS.md#files-on-disk)); only values that differ from
the default are written.
