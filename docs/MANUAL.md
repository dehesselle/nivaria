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
  pane borders show what the session actually resolved.
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
╭─ Account ─────────╮╭─────────────────────────────────────────────────────╮
│  MYDB (12)        ││                                                     │
│  OTHERDB (5)      ││              Detail pane                            │
│   Object tree     ││       (hosts exactly one detail view;               │
│                   ││   drag this border to resize either pane)           │
│                   ││                                                     │
╰───────── Role ────╯╰──────────────────────────────── WH: Warehouse ───────╯
  , Menu  / Search  F5 Refresh                                  12  (2/5)
                              Status footer
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
| **Object tree** | The browser. One top-level node per selected database, showing its schema count (`MYDB (12)`), expanding into schemas → `Tables (n)` / `Views (n)` / … → object. Its border shows the **Account** at the top and the **Role** at the bottom. |
| **Search modal** | Find-as-you-type over every cached object and schema across every selected database. |
| **Detail pane** | Shows one detail view for whatever node the cursor is on. Its border with the Object tree can be dragged to resize both panes. Its bottom border shows the session's **Warehouse**. |
| **Object view** | The detail view for one object (e.g. Table view, Pipe view). Category, schema and database nodes have views of their own (see "Detail views"). |
| **Status footer** | Key bindings on the left, starting with `, Menu` and `^q Quit`; **Countdown** and **Pool gauge** on the right. |
| **Chord panel** | Hidden until you pause partway through a chord, then covers the Status footer and lists what can come next (see "Chords"). |
| **Log panel** | Hidden until `F1`, then slides down over the top third of the screen. `F1` hides it again. It has its own level and source filters. It starts at level Info, where it shows the journal (see "The journal"); set it to Debug to see every statement nivaria sends from then on, live. |

The tree takes a quarter of the width at start. A dragged width is not kept
across sessions.

## Keys

| Key | Action |
| --- | --- |
| `,` | Start a chord (see "Chords") |
| `F1` | Show or hide the Log panel |
| `/` | Open search |
| `F5` | Refresh what the screen shows now, whatever its age: what the detail pane shows, and the list the cursor's node is in |
| `Ctrl+Q` | Quit; press again to quit without waiting for running statements |

## Chords

Commands are typed as a sequence of keys starting with `,`, one key after the
other.

| Keys | Action |
| --- | --- |
| `, a c` | Connect: reopen the connect dialog (reconnect, or switch account) |
| `, a w` | Warehouse selection |
| `, a r` | Role selection |
| `, a d` | Database selection |
| `, a i` | Info: the session's account, role and warehouse in full, and whether it uses PrivateLink |
| `, w w` | Open Workspaces in Snowsight, to write SQL |
| `, w o` | Open the object under the cursor in Snowsight |
| `, e` | Export the table on screen as a CSV (see "Exporting a table") |
| `, s` | Settings |
| `, ?` | About |

`a` is the **Account** group and `w` the **Web** group.

Pause after any key and the Chord panel lists what can come next, and waits for
you. While it is up, `backspace` steps back and `escape` closes it. Clicking an
entry presses its key, and clicking `, Menu` in the Status footer starts a chord.

A key that continues no chord abandons it and does nothing else. Chords belong
to the main screen: in a dialog, `,` is an ordinary comma.

## Browsing

`, a d` opens the **Databases picker**: type to filter, `space` or a click
toggles a database, and `enter` or **Ok** confirms. Each selected database gets
its own top-level node, and the selection is remembered per connection.

Expanding a node lists its children from the cache at once, and requests a
refresh in the background if the cache is missing or old; the tree updates in
place without moving the cursor. Counts on collapsed nodes come from the cache.

Moving the cursor shows the node in the Detail pane. `enter` on a row of a
category list (for example `Tables (n)`) opens that object and moves the tree
cursor to it.

`, w o` opens the tree cursor's database, schema or object in Snowsight in the
default browser; on a category such as `Pipes (n)` it opens the schema. `, w w`
opens Snowsight's Workspaces page, wherever the cursor is. Add or pick a SQL file
there: Snowflake documents no link that creates one. Snowsight has its own
session and role, so your role in nivaria does not carry over.

Both links use the organization and account name nivaria reads from Snowflake
when connecting, so any form of account identifier works, and open the public
`app.snowflake.com`. A PrivateLink account (an identifier ending in
`.privatelink`) reaches Snowsight through a private host that depends on its
network setup, so both ask for **Snowsight URL** in Settings instead: enter the
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

Search covers what is cached. The objects of a selected database are fetched
in the background the first time, without browsing it; the footer spinner runs
until that is done. A search opened before then shows what has arrived so far,
and reopening it shows the rest. Column names are not searched.

## Detail views

Tabbed views switch tabs with a letter key while focus is in the view; a
letter means the same tab everywhere.

| Node | View | Tabs (key) |
| --- | --- | --- |
| Database | Database view | Grants (`g`), Roles (`r`), Tags (`t`) |
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
- **Roles** (Database view) lists the database's database roles: owner, how
  many roles and database roles each is granted to, how many database roles
  it holds itself, when it was created and its comment. They are read only
  while this tab is open.
- **History** merges a pipe's load or a task's run history from two Snowflake
  sources. A failed load or run shows its error below the table. A note above
  says when the history was capped. While the slower, older source is still
  being read the first time, the note says what is known so far, such as "No
  run history in the last 7 days. Still looking...". A pipe's recent loads are
  read through its target table. When the table is not known from a load and
  the pipe's definition does not name it in full (`database.schema.table`),
  only `ACCOUNT_USAGE` is read, and a line above the history says it can be up
  to 2 hours behind. When one of the two sources cannot be read, for instance
  because the target table was dropped or your role may not see it, the note
  says which part of the history is missing and why.
- **Tags** (Database and Schema view) lists the tags applied to it. They are
  read only while this tab is open.
- **Activity** (Pipe list) shows each pipe's latest load. It comes from
  `ACCOUNT_USAGE`, which Snowflake says can be up to 2 hours behind, and a
  line above the table gives that lag. The Task list's last-run columns come
  from there too (up to 45 minutes behind) and carry the same line.
- **Status** (Pipe list) shows each pipe's `invalid_reason` and the
  execution state Snowflake reports for it (`RUNNING`, `PAUSED`,
  `STOPPED_...`), with the files waiting to be loaded (Pending Files). Both
  are read, 50 pipes at a time, only while this tab is open; a pipe whose
  state has not been read yet shows none.

Columns sourced from something other than `SHOW` (Last DDL, Last Altered,
pipe and task activity, tags, history, data) stay blank when the session has
no warehouse or lacks the privilege; see
[OPERATIONS.md](OPERATIONS.md#warehouse-and-privileges). Picking a warehouse
fills them in. A line above the list names such columns while they are first
being fetched ("Last DDL, Last Altered: looking...") and says why they are
blank when that failed. The Status tab counts the pipes whose execution state
has been read so far. The Grants, Roles, Tags and Data tabs say when they are
still looking, when there is nothing to show, or why they could not fetch.

When a refresh fails, the rows already shown stay, and a note above the list,
History or Data table says when they were fetched.

Hovering a shortened cell in a list or a history shows its raw value: a timestamp
in full, the exact row count or size in bytes.

## Exporting a table

`, e` saves the table the Detail pane shows as a CSV in your Downloads folder, and
a message says where. It works wherever the focus is. There is nothing to export on
a SQL tab or from an empty table, and nivaria says so instead of writing a file.

**The Data tab is not exported.** Its rows can be personal data, and nivaria does
not write them to disk.

The file holds the rows nivaria has at that moment, in the order shown, and sends
nothing to Snowflake. A cell shortened on screen is written with its raw value: a
timestamp in full with its UTC offset, the exact row count, the size in bytes. A
few columns are written as shown because they have no single raw value: a grantee's
Privileges, the Tags count, and Materialized. What the panel below a table shows,
such as a load's error, is not in the file.

The filename says what the file is and when it was taken, so a later export never
replaces an earlier one:

```text
partial_pipe_history_MYDB.PUBLIC.LOAD_ORDERS_20261107-143005.csv
table_columns_MYDB.PUBLIC.ORDERS_20261107-143005.csv
```

**`partial_` means the file may not hold every row Snowflake has.** It is there
unless everything the table is filled from has finished without hitting a row cap.
A pipe's or task's history always has it, because a history only ever covers a time
window. Any other table has it while one of its sources is still being fetched or
after one failed, so exporting again later can give a file without it.

The file is UTF-8 with a byte order mark, so that Excel reads it correctly, and the
**CSV delimiter** setting decides what separates its columns.

Values are written as they are. A spreadsheet evaluates a cell starting with `=`,
and object names, comments and file names can start with one.

## The journal

The journal is a short, timestamped account of what you did and what nivaria
fetched because of it. It appears in the Log panel (`F1`) as the source
`nivaria > journal`, which the panel's source filter can hide:

```text
user  cursor rested on pipe DB.S.LOAD_ORDERS
queue pipe_activity:DB.S  cursor settled: stale, age 6m12s, cache age 5m [interactive]
send  pipe_activity:DB.S  ACCOUNT_USAGE.COPY_HISTORY, latest row per pipe, window 4h12m [cursor settled, nivaria-fetch-0]
done  pipe_activity:DB.S  12 rows, 2.2 s
```

| Line | Means |
| --- | --- |
| `user` | Something you did. |
| `queue` | A fetch was asked for: the trigger, why the scope was due, and at which priority. |
| `send` | A statement goes to Snowflake now, and the trigger that asked for it. |
| `done` / `fail` | How it ended. |
| `conn` | A login, or a `USE ROLE` / `USE WAREHOUSE`. |
| `note` | Anything else, such as a sweep starting. |
| `skip` / `drop` | Verbose only: asked for and not fetched, and why, or thrown away. |

The trigger is a word from the Journal column of
[data-retrieval.md](data-retrieval.md#triggers), so a line can be checked
against what that document says should happen.

The panel keeps the journal in memory only, so it is gone when nivaria exits.
**write the journal to a file** keeps it in nivaria's cache directory as well,
one file per session (see [OPERATIONS.md](OPERATIONS.md#files-on-disk)).

### Sharing a journal

**pseudonymize the journal file** writes placeholders to the file instead of
the names of databases, schemas, objects, roles, warehouses and the account,
so you can share a journal from a production account:

```text
user  cursor rested on pipe DB_1.SCH_2.PIPE_3
send  pipe_activity:DB_1.SCH_2  ACCOUNT_USAGE.COPY_HISTORY, latest row per pipe, window 4h12m [cursor settled, nivaria-fetch-0]
```

- **The same name always gets the same number**, in every file. The number
  belongs to the name and the prefix to its kind, so a table and a pipe that
  share a name read `TABLE_12` and `PIPE_12`. `OBJ` is a table or a view.
- **Snowflake's own names stay**: `SNOWFLAKE`, `SNOWFLAKE_SAMPLE_DATA`,
  `INFORMATION_SCHEMA`, `PUBLIC` and the system roles.
- **Error messages are scrubbed**: known names are replaced, an unknown quoted
  name becomes `?`, and e-mail addresses and hosts become `<email>` and
  `<host>`. An unquoted name nivaria has not journalled before can still slip
  through, so read the `fail` lines before you share a file.
- **The setting applies to the lines written from then on.** Switch it on
  before the session you want to share, or start a new one.
- The Log panel keeps the real names.

`journal/names.txt` in the cache directory says which name each number stands
for, one `12 PIPE "LOAD_ORDERS"` per line. It stays on your machine (see
[OPERATIONS.md](OPERATIONS.md#files-on-disk)).

## Session labels

- **Account** (top of the Object tree's border): the account's name, for
  public and PrivateLink accounts alike. If Snowflake did not report the name
  when connecting, it is the account identifier up to its first dot.
- **Role** (bottom of the Object tree's border): the session's current role.
- **Warehouse** (bottom of the Detail pane's border): `WH:` and the session's
  current warehouse. It can read `WH: none`.

A long name is cut off when the pane is too narrow. `, a i` opens **Info**, which
shows all of them in full, plus whether the account identifier uses PrivateLink.
**Ok**, `enter` or `escape` closes it.

## Status footer

- **Countdown**: seconds until the first thing on screen is due a refresh;
  nothing on screen goes stale before it reaches zero. It jumps as what is on
  screen changes, turns into a spinner while something on screen is being
  refetched, and is blank when the cache age setting is `0`.
- **Pool gauge**: `(in use / total)` connections.

## Settings

**Settings** (`, s`) lists every setting by group, each editable in place. **OK** validates, saves and closes; **Apply** saves and stays open;
**Cancel** or `escape` discards, asking first if anything changed. Clearing a
field and confirming resets it to its default. A change takes effect without a
restart.

| Setting | Default | Effect |
| --- | --- | --- |
| cache age | 300 | Seconds cached metadata stays fresh before what is on screen is refreshed in the background. `0` disables background refreshing; `F5` still works. |
| cursor settle delay (milliseconds) | 250 | How long the tree cursor has to rest on a node before what it shows is checked and fetched if needed. What is cached shows at once either way. `0` means no wait. |
| connection pool size | 3 | Connections to keep open (1–5). Every connection may cost an MFA prompt. |
| Snowsight URL | empty | Per account: the Snowsight address that `, w w` and `, w o` open, up to the account name. Empty uses the public address; a PrivateLink account needs it set. |
| fetch page size | 1000 | Rows one fetch job asks for. Lowering it mainly makes fetching slower. |
| pipe history fetch window (days) | 90 | Days of pipe load history fetched and cached. |
| pipe history display window (days) | 90 | Days of it shown in the Pipe view. |
| task history fetch window (days) | 90 | Days of task run history fetched and cached. |
| task history display window (days) | 90 | Days of it shown in the Task view. |
| history row cap | 5000 | Most rows one history statement returns; the newest are kept. |
| ACCOUNT_USAGE lag margin (x published lag) | 2 | How far back each refresh of pipe and task activity reaches before the previous one, as a multiple of Snowflake's published lag (2 hours for loads, 45 minutes for task runs): 2, 3 or 4. A load or run Snowflake publishes later than that is not picked up. |
| data preview rows | 100 | Rows the Data tab fetches. |
| data preview timeout (seconds) | 15 | How long a Data tab query may run before it is cancelled. `0` means no limit. |
| max pages per refresh | 50 | Pages one refresh may fetch before it gives up with an error. |
| result row ceiling | 200000 | Most rows read from any single statement before it is abandoned with an error. `0` disables it. |
| prime chunk threshold | 10000 | Result size above which a whole-database fetch is redone per schema. |
| CSV delimiter | operating system | What separates the columns of an export: `comma`, `semicolon`, or `operating system`, which is what Excel follows when it opens a CSV. That is the list separator in Windows' region settings. macOS and Linux have no such setting, so there it is a semicolon where the locale's decimal point is a comma, otherwise a comma. |
| write the journal to a file | off | Also write the journal to a file, one per session (see "The journal"). |
| pseudonymize the journal file | off | Write placeholders such as `DB_3.SCH_7.PIPE_12` to the journal file instead of names (see "Sharing a journal"). |
| verbose journal | off | Also journal what was asked for and not fetched. The Log panel shows these lines at level Debug. |

Settings live in `settings.json` (see
[OPERATIONS.md](OPERATIONS.md#files-on-disk)); only values that differ from
the default are written.
