# Data retrieval

This document lists what nivaria sends to Snowflake on each screen and for each
trigger. It is the blueprint to check the implementation against: where the two
disagree, one of them is wrong.

Why each source was chosen is in [DESIGN.md](DESIGN.md#fetching-what-when-and-how),
the limits on what comes back in
[OPERATIONS.md](OPERATIONS.md#limits-and-timeouts).

## How to read the tables

Unless a row says otherwise, **a statement is sent only if its scope was never
fetched or is older than cache age.** Otherwise the screen paints from sqlite and
sends nothing. Painting never waits on Snowflake.

**"Selected" rows wait for the cursor to settle.** The tree cursor has to rest on a
node for the **cursor settle delay** before anything asks whether that node needs
fetching. Moving through a list therefore sends nothing for the rows it passes.

Words used in the Screen column:

- **Selected:** the tree cursor is on the node. Arrow keys are enough, no Enter
  needed.
- **Added:** ticked in the database dialog, or restored from the profile.

Other columns:

- **Scope** is nivaria's `ScopeType`; **Reach** is how much of the account the
  statement covers.
- **Needs:** `WH` is a warehouse, `IP` is `IMPORTED PRIVILEGES` on `SNOWFLAKE`.
- **First** and **Refresh** are the statement's duration for a first fetch and for a
  later one. `?` means it has not been measured.
- `<type>` is `TABLES`, `VIEWS`, `PIPES`, `STAGES` or `TASKS`.

## Screens

| Screen | Data source | Scope | Reach | Needs | First | Refresh | When | Feeds |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Connect | `SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT_NAME()` | none | session | none | ? | n/a | always, once per connect | Snowsight links, account label, Info dialog |
| Startup | `SHOW DATABASES` | `DATABASES` | account | none | ~0.2 s | n/a | always, also after a role switch or reconnect | database dialog |
| Startup | `SHOW WAREHOUSES` | `WAREHOUSES` | account | none | ~0.2 s | n/a | always, also after a role switch or reconnect | warehouse dialog |
| Startup | `SHOW ROLES` | `ROLES` | account | none | ~0.2 s | n/a | always, also after a role switch or reconnect | role dialog |
| Database added | `SHOW SCHEMAS IN DATABASE` | `SCHEMAS` | database | none | ~0.2 s | ~0.2 s | | tree, search |
| Database added | `SHOW <type> IN DATABASE`, 5 statements | `DATABASE_PRIME` | database | none | ~1 s each | none | background; only types with a never-fetched listing; needs the schema list | tree, lists, search |
| Database expanded | `SHOW SCHEMAS IN DATABASE` | `SCHEMAS` | database | none | ~0.2 s | ~0.2 s | | tree, search |
| Schema expanded | `SHOW <type> IN DATABASE`, 5 statements | `DATABASE_PRIME` | database | none | ~1 s each | ~1 s each | visible priority | tree, lists, search |
| Category expanded | `SHOW <type> IN DATABASE` | `DATABASE_PRIME` | database | none | ~1 s | ~1 s | | tree, lists, search |
| Database selected | `SHOW GRANTS ON DATABASE` | `GRANTS` | database | none | ~0.2 s | ~0.2 s | | Grants tab |
| Database selected | `INFORMATION_SCHEMA.TAG_REFERENCES` | `TAG_REFERENCES` | database | WH | ? | ? | Tags tab shown | Tags tab |
| Schema selected | `SHOW GRANTS ON SCHEMA` | `GRANTS` | schema | none | ~0.2 s | ~0.2 s | | Grants tab |
| Schema selected | `INFORMATION_SCHEMA.TAG_REFERENCES` | `TAG_REFERENCES` | schema | WH | ? | ? | Tags tab shown | Tags tab |
| Tables (n) selected | `SHOW TABLES IN DATABASE` | `DATABASE_PRIME` | database | none | ~1 s | ~1 s | | Tables list |
| Tables (n) selected | `INFORMATION_SCHEMA.TABLES`, `TABLE_TYPE = 'BASE TABLE'` | `TABLE_STATS` | schema | WH | ? | ? | | Last DDL, Last Altered |
| Views (n) selected | `SHOW VIEWS IN DATABASE` | `DATABASE_PRIME` | database | none | ~1 s | ~1 s | | Views list |
| Views (n) selected | `INFORMATION_SCHEMA.TABLES`, `TABLE_TYPE = 'VIEW'` | `VIEW_STATS` | schema | WH | ? | ? | | Last DDL, Last Altered |
| Views (n) selected | `SHOW MATERIALIZED VIEWS IN SCHEMA` | `MATERIALIZED_VIEW_STATS` | schema | none | ~0.2 s | ~0.2 s | | Rows, Size |
| Pipes (n) selected | `SHOW PIPES IN DATABASE` | `DATABASE_PRIME` | database | none | ~1 s | ~1 s | | Pipes list |
| Pipes (n) selected | `ACCOUNT_USAGE.COPY_HISTORY`, latest row per pipe | `PIPE_ACTIVITY` | schema | WH, IP | 8-31 s | ~2 s | any tab | Activity tab, target table resolution |
| Pipes (n) selected | `SYSTEM$PIPE_STATUS`, batches of 50 | `PIPE_STATUS` | schema | none | ~25 ms per pipe | ~25 ms per pipe | Status tab shown, pipe list fetched | Execution State, Pending Files |
| Stages (n) selected | `SHOW STAGES IN DATABASE` | `DATABASE_PRIME` | database | none | ~1 s | ~1 s | | Stages list |
| Tasks (n) selected | `SHOW TASKS IN DATABASE` | `DATABASE_PRIME` | database | none | ~1 s | ~1 s | | Tasks list |
| Tasks (n) selected | `ACCOUNT_USAGE.TASK_HISTORY`, latest row per task | `TASK_ACTIVITY` | schema | WH, IP | ? | ? | | Last Scheduled, Last Run State |
| Table or view selected | `SHOW COLUMNS IN TABLE` | `COLUMNS` | one object | none | ~0.2 s | ~0.2 s | | Columns tab |
| Table or view selected | `INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS` | `COLUMN_TAG_REFERENCES` | one object | WH | ? | ? | any tab | Columns tab's Tags column and detail panel |
| Table or view selected | `SHOW GRANTS ON TABLE` or `ON VIEW` | `GRANTS` | one object | none | ~0.2 s | ~0.2 s | Grants tab shown | Grants tab |
| Table or view selected | `SELECT * ... LIMIT 100` | `DATA_PREVIEW` | one object | WH | ?, 15 s timeout | same | Data tab shown | Data tab |
| Pipe selected | `ACCOUNT_USAGE.COPY_HISTORY`, latest row per pipe | `PIPE_ACTIVITY` | **schema** | WH, IP | 8-31 s | ~2 s | always | target table resolution, whether the backfill is needed, the answer for a pipe with no loads |
| Pipe selected | `ACCOUNT_USAGE.COPY_HISTORY`, one pipe | `PIPE_ACTIVITY_HISTORY` | one object | WH, IP | 3-7 s | ~1.2 s | `PipeView.needs_backfill` | History tab, older half |
| Pipe selected | `INFORMATION_SCHEMA.COPY_HISTORY` | `PIPE_HISTORY` | one object | WH | ~0.2 s | ~0.2 s | target table resolved | History tab, recent half |
| Task selected | `ACCOUNT_USAGE.TASK_HISTORY`, one task | `TASK_ACTIVITY_HISTORY` | one object | WH, IP | 4-10 s | ? | always | History tab, older half |
| Task selected | `INFORMATION_SCHEMA.TASK_HISTORY` | `TASK_HISTORY` | one object | WH | ~0.2 s | ~0.2 s | always | History tab, recent half |
| Table, view, pipe or task selected | `SHOW <type> IN DATABASE` | `DATABASE_PRIME` | database | none | ~1 s | ~1 s | the object's listing is stale when the view asks | its SQL tab, the Data tab's row count |
| Stage selected, Search, Settings, any SQL tab | nothing | none | | | | | reads the cache only | |

Durations, from [measurements.md](measurements.md):

- `~0.2 s` is the measured `SHOW` round trip, and `~1 s` is the 2145-table prime.
- The refresh figures for the `ACCOUNT_USAGE` scopes are 2-day windows. A real
  refresh asks for less, about 4 h for pipes and 1.5 h for tasks.
- Pipe and task timings mix production and sandbox runs.

## Triggers

| Trigger | What gets sent | Priority |
| --- | --- | --- |
| Cursor settles | The "selected" rows above that are never fetched or stale | interactive |
| Node expanded or database added | The matching rows above that are never fetched or stale, at once | interactive, unless a row says otherwise |
| Tab opened, or a fetch lands and repaints | The rows that view now needs, at once | interactive |
| Cursor moves on | Nothing. Queued work only the previous view was waiting on is dropped, except listings. A statement already running finishes. | n/a |
| Sweep (countdown at zero) | Every stale scope in the tree, collapsed nodes included, plus the detail view's once the cursor has settled. A stale listing goes as its prime. Never opens a connection. | background |
| `F5` | Everything the screen shows now, whatever its age: the detail view's scopes and the tree node's listing, for an object its parent category's, for a schema its database's. A listing goes per schema (`SHOW <type> IN SCHEMA`), not as a prime. Log scopes stay window passes. | interactive |
| `full_refresh` (no key yet) | The detail view's log scopes, as full passes | interactive |
| Warehouse switch | `USE WAREHOUSE` on each connection, then whatever failed on screen | visible |
| Role switch | `USE ROLE` on each connection, then the Startup rows against that role's cache file. Clears the added databases. | interactive |
| Statement fails with "does not exist" | The listing one level up, per schema rather than as a prime | same as the failed job |
| Statement fails otherwise | Nothing at once. Retried one cache age later, or when the cursor comes back. | background, or interactive on return |
| Prime reaches the chunk threshold (10 000 rows) | `SHOW <type> IN SCHEMA`, one per schema | same as the prime |
