-- SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
--
-- SPDX-License-Identifier: 0BSD

-- nivaria cache.
--
-- The bottom layer mirrors Snowflake: one table per SHOW command, with the
-- result columns named exactly as Snowflake returns them. Collapsing the
-- different object kinds into one generic table would invent terminology and
-- lose columns (SHOW PIPES has definition/notification_channel, SHOW TABLES
-- has rows/bytes/cluster_by), so the repetition below is the cheaper price.
--
-- The column sets were verified against a live account on 2026-09-05
-- (Snowflake 10.31.103) with tools/probe_snowflake.py. Each SHOW returns more
-- columns than are mirrored here; the subset kept is what a browser can show
-- or filter on. Anything not listed is dropped on ingest rather than failing,
-- so Snowflake adding a column is a non-event.
--
-- There is no migration system here: every statement is CREATE TABLE IF NOT
-- EXISTS, so a change never reaches a cache file that already exists. Any
-- edit to this file or search.sql bumps SCHEMA_VERSION in db.py instead, and
-- a file stamped with another version is deleted and rebuilt on open.
-- nivaria is pre-1.0 with no backward-compatibility policy, so nothing is
-- carried over.
--
-- Bookkeeping columns are underscore-prefixed so they can never collide with
-- a Snowflake column name:
--   _scope       which cache_entry row owns this row
--   _generation  the refresh pass that last wrote it (see mark-and-sweep)
--   _fetched_at  when that pass wrote it

CREATE TABLE IF NOT EXISTS show_databases (
    name            TEXT NOT NULL PRIMARY KEY,
    created_on      TEXT,
    kind            TEXT,
    owner           TEXT,
    owner_role_type TEXT,
    comment         TEXT,
    options         TEXT,
    origin          TEXT,
    retention_time  TEXT,
    _scope          TEXT    NOT NULL,
    _generation     INTEGER NOT NULL,
    _fetched_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS show_databases_scope ON show_databases(_scope);

CREATE TABLE IF NOT EXISTS show_schemas (
    database_name   TEXT NOT NULL,
    name            TEXT NOT NULL,
    created_on      TEXT,
    owner           TEXT,
    owner_role_type TEXT,
    comment         TEXT,
    options         TEXT,
    retention_time  TEXT,
    _scope          TEXT    NOT NULL,
    _generation     INTEGER NOT NULL,
    _fetched_at     REAL    NOT NULL,
    PRIMARY KEY (database_name, name)
);
CREATE INDEX IF NOT EXISTS show_schemas_scope ON show_schemas(_scope);

-- The is_* family is mirrored because those flags decide what a "table" even
-- is here: an Iceberg, dynamic, hybrid or external table behaves differently
-- enough that a browser showing them all as "TABLE" would be misleading.
CREATE TABLE IF NOT EXISTS show_tables (
    database_name        TEXT NOT NULL,
    schema_name          TEXT NOT NULL,
    name                 TEXT NOT NULL,
    created_on           TEXT,
    kind                 TEXT,
    comment              TEXT,
    cluster_by           TEXT,
    rows                 INTEGER,
    bytes                INTEGER,
    owner                TEXT,
    owner_role_type      TEXT,
    retention_time       TEXT,
    automatic_clustering TEXT,
    change_tracking      TEXT,
    search_optimization  TEXT,
    is_external          TEXT,
    is_event             TEXT,
    is_hybrid            TEXT,
    is_iceberg           TEXT,
    is_dynamic           TEXT,
    _scope               TEXT    NOT NULL,
    _generation          INTEGER NOT NULL,
    _fetched_at          REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_tables_scope ON show_tables(_scope);

CREATE TABLE IF NOT EXISTS show_views (
    database_name   TEXT NOT NULL,
    schema_name     TEXT NOT NULL,
    name            TEXT NOT NULL,
    created_on      TEXT,
    comment         TEXT,
    owner           TEXT,
    owner_role_type TEXT,
    text            TEXT,
    is_secure       TEXT,
    is_materialized TEXT,
    change_tracking TEXT,
    _scope          TEXT    NOT NULL,
    _generation     INTEGER NOT NULL,
    _fetched_at     REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_views_scope ON show_views(_scope);

-- invalid_reason is mirrored deliberately: a stopped pipe is the single most
-- useful thing to surface about one, and it is only in the SHOW output.
CREATE TABLE IF NOT EXISTS show_pipes (
    database_name        TEXT NOT NULL,
    schema_name          TEXT NOT NULL,
    name                 TEXT NOT NULL,
    created_on           TEXT,
    definition           TEXT,
    owner                TEXT,
    owner_role_type      TEXT,
    notification_channel TEXT,
    comment              TEXT,
    pattern              TEXT,
    integration          TEXT,
    error_integration    TEXT,
    invalid_reason       TEXT,
    kind                 TEXT,
    is_snowflake_managed TEXT,
    _scope               TEXT    NOT NULL,
    _generation          INTEGER NOT NULL,
    _fetched_at          REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_pipes_scope ON show_pipes(_scope);

CREATE TABLE IF NOT EXISTS show_stages (
    database_name       TEXT NOT NULL,
    schema_name         TEXT NOT NULL,
    name                TEXT NOT NULL,
    created_on          TEXT,
    url                 TEXT,
    type                TEXT,
    owner               TEXT,
    owner_role_type     TEXT,
    comment             TEXT,
    region              TEXT,
    cloud               TEXT,
    storage_integration TEXT,
    endpoint            TEXT,
    has_credentials     TEXT,
    directory_enabled   TEXT,
    _scope              TEXT    NOT NULL,
    _generation         INTEGER NOT NULL,
    _fetched_at         REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_stages_scope ON show_stages(_scope);

CREATE TABLE IF NOT EXISTS show_tasks (
    database_name     TEXT NOT NULL,
    schema_name       TEXT NOT NULL,
    name              TEXT NOT NULL,
    created_on        TEXT,
    definition        TEXT,
    owner             TEXT,
    owner_role_type   TEXT,
    comment           TEXT,
    warehouse         TEXT,
    schedule          TEXT,
    state             TEXT,
    predecessors      TEXT,
    condition         TEXT,
    error_integration TEXT,
    last_committed_on TEXT,
    last_suspended_on TEXT,
    _scope            TEXT    NOT NULL,
    _generation       INTEGER NOT NULL,
    _fetched_at       REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_tasks_scope ON show_tasks(_scope);

-- Columns of one table. See gateway/queries.py for which statement fills this;
-- the column names follow INFORMATION_SCHEMA.COLUMNS, upper case as Snowflake
-- returns them, and SHOW COLUMNS output is mapped onto the same shape.
-- _desc_type is the exception: the full type as DESC TABLE prints it, which
-- no INFORMATION_SCHEMA field can hold, hence nivaria's underscore.
CREATE TABLE IF NOT EXISTS information_schema_columns (
    TABLE_CATALOG            TEXT NOT NULL,
    TABLE_SCHEMA             TEXT NOT NULL,
    TABLE_NAME               TEXT NOT NULL,
    COLUMN_NAME              TEXT NOT NULL,
    ORDINAL_POSITION         INTEGER,
    DATA_TYPE                TEXT,
    IS_NULLABLE              TEXT,
    COLUMN_DEFAULT           TEXT,
    CHARACTER_MAXIMUM_LENGTH INTEGER,
    NUMERIC_PRECISION        INTEGER,
    NUMERIC_SCALE            INTEGER,
    DATETIME_PRECISION       INTEGER,
    COMMENT                  TEXT,
    _desc_type               TEXT,
    _scope                   TEXT    NOT NULL,
    _generation              INTEGER NOT NULL,
    _fetched_at              REAL    NOT NULL,
    PRIMARY KEY (TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME)
);
CREATE INDEX IF NOT EXISTS information_schema_columns_scope
    ON information_schema_columns(_scope);

-- LAST_DDL/LAST_ALTERED for the "Tables (n)" list, sourced via a real SELECT
-- against INFORMATION_SCHEMA.TABLES (needs a running warehouse) rather than
-- SHOW -- see docs/DESIGN.md "A second source". LAST_DDL is a pure DDL
-- timestamp; LAST_ALTERED is the closest available proxy for "last DML" --
-- it also covers DDL and background metadata operations, so the UI labels
-- it "Last Altered", not "Last DML". Column names and values verified live
-- on 2026-09-09 (docs/snowflake-behavior.md).
CREATE TABLE IF NOT EXISTS information_schema_tables (
    TABLE_CATALOG TEXT NOT NULL,
    TABLE_SCHEMA  TEXT NOT NULL,
    TABLE_NAME    TEXT NOT NULL,
    LAST_DDL      TEXT,
    LAST_ALTERED  TEXT,
    _scope        TEXT    NOT NULL,
    _generation   INTEGER NOT NULL,
    _fetched_at   REAL    NOT NULL,
    PRIMARY KEY (TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME)
);
CREATE INDEX IF NOT EXISTS information_schema_tables_scope
    ON information_schema_tables(_scope);

-- LAST_DDL/LAST_ALTERED for the "Views (n)" list, the same
-- INFORMATION_SCHEMA.TABLES statement information_schema_tables above uses,
-- just filtered to TABLE_TYPE = 'VIEW' instead of 'BASE TABLE' -- see
-- ScopeType.VIEW_STATS. Kept as its own physical table rather than sharing
-- information_schema_tables under a second _scope value: table and view
-- names cannot collide within one schema, so it would technically be safe,
-- but this repo already keeps near-identical shapes in separate physical
-- tables (account_usage_copy_history vs account_usage_task_history) rather
-- than lean on that, and it costs nothing here since neither table is large.
-- Column names and values verified live on 2026-09-09: every user view
-- probed carries non-null LAST_DDL/LAST_ALTERED, while Snowflake's own
-- INFORMATION_SCHEMA system views read back NULL for both
-- (docs/snowflake-behavior.md).
CREATE TABLE IF NOT EXISTS information_schema_views (
    TABLE_CATALOG TEXT NOT NULL,
    TABLE_SCHEMA  TEXT NOT NULL,
    TABLE_NAME    TEXT NOT NULL,
    LAST_DDL      TEXT,
    LAST_ALTERED  TEXT,
    _scope        TEXT    NOT NULL,
    _generation   INTEGER NOT NULL,
    _fetched_at   REAL    NOT NULL,
    PRIMARY KEY (TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME)
);
CREATE INDEX IF NOT EXISTS information_schema_views_scope
    ON information_schema_views(_scope);

-- Rows/bytes for materialized views in a schema -- SHOW VIEWS (show_views
-- above) never carries these, even for materialized views; SHOW MATERIALIZED
-- VIEWS is Snowflake's separate command that does, with its own column set
-- (see ScopeType.MATERIALIZED_VIEW_STATS). Metadata layer, needs no
-- warehouse, unlike information_schema_views/information_schema_tables.
-- Column names and values verified live on 2026-09-09
-- (docs/snowflake-behavior.md): the full column list is created_on, name,
-- reserved, database_name, schema_name, cluster_by, rows, bytes,
-- source_database_name, source_schema_name, source_table_name,
-- refreshed_on, compacted_on, owner, invalid, invalid_reason, behind_by,
-- comment, text, is_secure, automatic_clustering, owner_role_type --
-- rows/bytes are real columns here, confirming they exist where SHOW VIEWS
-- never carries them, and they read back the materialized view's real row
-- count and size.
CREATE TABLE IF NOT EXISTS show_materialized_views (
    database_name TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    name          TEXT NOT NULL,
    rows          INTEGER,
    bytes         INTEGER,
    _scope        TEXT    NOT NULL,
    _generation   INTEGER NOT NULL,
    _fetched_at   REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_materialized_views_scope
    ON show_materialized_views(_scope);

-- Every pipe's load history in a schema (bounded by the "pipe history fetch
-- window" setting), sourced via a real SELECT against
-- SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY (needs a running warehouse) rather
-- than SHOW -- see docs/DESIGN.md "A second source", the second instance of
-- the pattern information_schema_tables established. Unlike that first
-- instance, the source here is ACCOUNT_USAGE rather than INFORMATION_SCHEMA:
-- it also needs IMPORTED PRIVILEGES on the SNOWFLAKE database (or
-- ACCOUNTADMIN) and can lag real activity by up to ~2 hours -- either gap
-- just leaves these columns blank, same as a missing warehouse does. Column
-- names and values verified live on 2026-09-09 (docs/snowflake-behavior.md).
--
-- One row per pipe -- reduced from one row per historical load by a QUALIFY
-- in queries.pipe_activity, keeping only each pipe's latest. This backs the
-- "Pipes (n)" list and Repository.pipe_target_table's resolution, neither of
-- which needs more than the newest row. A single pipe's own full history is
-- account_usage_copy_history_detail below, fetched separately and lazily.
CREATE TABLE IF NOT EXISTS account_usage_copy_history (
    PIPE_CATALOG_NAME       TEXT NOT NULL,
    PIPE_SCHEMA_NAME        TEXT NOT NULL,
    PIPE_NAME               TEXT NOT NULL,
    FILE_NAME               TEXT,
    FILE_SIZE               INTEGER,
    ROW_COUNT               INTEGER,
    STATUS                  TEXT,
    LAST_LOAD_TIME          TEXT,
    TABLE_CATALOG_NAME      TEXT,
    TABLE_SCHEMA_NAME       TEXT,
    TABLE_NAME              TEXT,
    FIRST_ERROR_MESSAGE     TEXT,
    FIRST_ERROR_LINE_NUMBER INTEGER,
    _scope                  TEXT    NOT NULL,
    _generation             INTEGER NOT NULL,
    _fetched_at             REAL    NOT NULL,
    PRIMARY KEY (PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME, FILE_NAME, LAST_LOAD_TIME)
);
CREATE INDEX IF NOT EXISTS account_usage_copy_history_scope
    ON account_usage_copy_history(_scope);

-- One pipe's full ACCOUNT_USAGE.COPY_HISTORY backfill (bounded by the "pipe
-- history fetch window" setting, same source and window as
-- account_usage_copy_history above, just not collapsed to the latest row).
-- Scoped per pipe, like information_schema_copy_history below, and fetched
-- just as lazily -- only once that pipe's detail view is open. This is a
-- separate table rather than the same one under a different _scope value:
-- account_usage_copy_history's primary key has no _scope column, so a row
-- shared between that schema-wide scope and a per-pipe scope here would let
-- whichever pass wrote it last steal its _scope tag out from under the
-- other. Column names and values verified live on 2026-09-09.
CREATE TABLE IF NOT EXISTS account_usage_copy_history_detail (
    PIPE_CATALOG_NAME       TEXT,
    PIPE_SCHEMA_NAME        TEXT,
    PIPE_NAME               TEXT,
    FILE_NAME               TEXT,
    FILE_SIZE               INTEGER,
    ROW_COUNT               INTEGER,
    STATUS                  TEXT,
    LAST_LOAD_TIME          TEXT,
    PIPE_RECEIVED_TIME      TEXT,
    TABLE_CATALOG_NAME      TEXT,
    TABLE_SCHEMA_NAME       TEXT,
    TABLE_NAME              TEXT,
    FIRST_ERROR_MESSAGE     TEXT,
    FIRST_ERROR_LINE_NUMBER INTEGER,
    _scope                  TEXT    NOT NULL,
    _generation             INTEGER NOT NULL,
    _fetched_at             REAL    NOT NULL,
    PRIMARY KEY (_scope, FILE_NAME, LAST_LOAD_TIME)
);
CREATE INDEX IF NOT EXISTS account_usage_copy_history_detail_scope
    ON account_usage_copy_history_detail(_scope);

-- One pipe's near-real-time load history (the last 14 days -- Snowflake's
-- own retention limit for the function backing this), sourced via a real
-- SELECT against INFORMATION_SCHEMA.COPY_HISTORY. Scoped per pipe, like
-- information_schema_columns is scoped per table -- _scope disambiguates
-- which pipe's rows these are, since the physical table holds every pipe's
-- history together. Column names and values verified live on 2026-09-09.
CREATE TABLE IF NOT EXISTS information_schema_copy_history (
    FILE_NAME               TEXT,
    FILE_SIZE               INTEGER,
    ROW_COUNT               INTEGER,
    STATUS                  TEXT,
    LAST_LOAD_TIME          TEXT,
    PIPE_RECEIVED_TIME      TEXT,
    PIPE_NAME               TEXT,
    FIRST_ERROR_MESSAGE     TEXT,
    FIRST_ERROR_LINE_NUMBER INTEGER,
    _scope                  TEXT    NOT NULL,
    _generation             INTEGER NOT NULL,
    _fetched_at             REAL    NOT NULL,
    PRIMARY KEY (_scope, FILE_NAME, LAST_LOAD_TIME)
);
CREATE INDEX IF NOT EXISTS information_schema_copy_history_scope
    ON information_schema_copy_history(_scope);

-- Every pipe's SYSTEM$PIPE_STATUS in a schema, one row per pipe. The
-- function takes one pipe as a constant and returns a JSON string, kept
-- here exactly as returned; the Pipes (n) list reads executionState out of
-- it. The three name columns are not Snowflake's output but the pipe each
-- call was made for, named like account_usage_copy_history's. Needs no
-- warehouse. Verified live on 2026-10-25 (docs/snowflake-behavior.md).
CREATE TABLE IF NOT EXISTS system_pipe_status (
    PIPE_CATALOG_NAME TEXT NOT NULL,
    PIPE_SCHEMA_NAME  TEXT NOT NULL,
    PIPE_NAME         TEXT NOT NULL,
    PIPE_STATUS       TEXT,
    _scope            TEXT    NOT NULL,
    _generation       INTEGER NOT NULL,
    _fetched_at       REAL    NOT NULL,
    PRIMARY KEY (PIPE_CATALOG_NAME, PIPE_SCHEMA_NAME, PIPE_NAME)
);
CREATE INDEX IF NOT EXISTS system_pipe_status_scope
    ON system_pipe_status(_scope);

-- Every task's latest run in a schema (bounded by the "task history fetch
-- window" setting), sourced via a real SELECT against
-- SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY (needs a running warehouse) rather
-- than SHOW -- see docs/DESIGN.md "A second source", the same pattern
-- account_usage_copy_history established. Needs IMPORTED PRIVILEGES on the
-- SNOWFLAKE database (or ACCOUNTADMIN) and can lag real activity by up to
-- ~45 minutes -- either gap just leaves these columns blank, same as a
-- missing warehouse does. Column names and values verified live on
-- 2026-09-09 (docs/snowflake-behavior.md).
--
-- One row per task -- reduced from one row per historical run by a QUALIFY
-- in queries.task_activity, keeping only each task's latest. A run is keyed
-- by (SCHEDULED_TIME, ATTEMPT_NUMBER), not QUERY_ID: QUERY_ID is NULL for a
-- run that failed before a query was submitted, and NULLs in a key never
-- collide, so a window pass re-reading its overlap would add such a run
-- again every time (docs/snowflake-behavior.md "Task history"). A task's
-- next run lands under a new key, and the window pass keeps only the newest
-- row per task (TableSpec.log). A single task's own full history is
-- account_usage_task_history_detail below, fetched separately and lazily.
CREATE TABLE IF NOT EXISTS account_usage_task_history (
    NAME              TEXT NOT NULL,
    DATABASE_NAME     TEXT NOT NULL,
    SCHEMA_NAME       TEXT NOT NULL,
    QUERY_ID          TEXT,
    SCHEDULED_TIME    TEXT NOT NULL,
    ATTEMPT_NUMBER    INTEGER NOT NULL,
    QUERY_START_TIME  TEXT,
    COMPLETED_TIME    TEXT,
    STATE             TEXT,
    ERROR_CODE        TEXT,
    ERROR_MESSAGE     TEXT,
    _scope            TEXT    NOT NULL,
    _generation       INTEGER NOT NULL,
    _fetched_at       REAL    NOT NULL,
    PRIMARY KEY (DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME, ATTEMPT_NUMBER)
);
CREATE INDEX IF NOT EXISTS account_usage_task_history_scope
    ON account_usage_task_history(_scope);

-- One task's full ACCOUNT_USAGE.TASK_HISTORY backfill (bounded by the "task
-- history fetch window" setting, same source and window as
-- account_usage_task_history above, just not collapsed to the latest row).
-- Scoped per task, like information_schema_task_history below, and fetched
-- just as lazily -- only once that task's detail view is open. This is a
-- separate table rather than the same one under a different _scope value,
-- for the same reason account_usage_copy_history_detail is separate from
-- account_usage_copy_history: the schema-wide table's primary key has no
-- _scope column. Column names and values verified live on 2026-09-09.
CREATE TABLE IF NOT EXISTS account_usage_task_history_detail (
    NAME              TEXT,
    DATABASE_NAME     TEXT,
    SCHEMA_NAME       TEXT,
    QUERY_ID          TEXT,
    SCHEDULED_TIME    TEXT NOT NULL,
    ATTEMPT_NUMBER    INTEGER NOT NULL,
    QUERY_START_TIME  TEXT,
    COMPLETED_TIME    TEXT,
    STATE             TEXT,
    ERROR_CODE        TEXT,
    ERROR_MESSAGE     TEXT,
    _scope            TEXT    NOT NULL,
    _generation       INTEGER NOT NULL,
    _fetched_at       REAL    NOT NULL,
    -- Not QUERY_ID, for the reason given on account_usage_task_history.
    PRIMARY KEY (_scope, SCHEDULED_TIME, ATTEMPT_NUMBER)
);
CREATE INDEX IF NOT EXISTS account_usage_task_history_detail_scope
    ON account_usage_task_history_detail(_scope);

-- One task's near-real-time run history (the last 7 days -- Snowflake's own
-- hard retention limit for the function backing this, shorter than
-- information_schema_copy_history's 14), sourced via a real SELECT against
-- INFORMATION_SCHEMA.TASK_HISTORY. Scoped per task, like
-- information_schema_copy_history is scoped per pipe -- _scope disambiguates
-- which task's rows these are, since the physical table holds every task's
-- history together. Unlike information_schema_copy_history, no target-table
-- resolution is needed to fetch this: the function accepts TASK_NAME/
-- DATABASE_NAME/SCHEMA_NAME as direct named arguments. Column names and
-- values verified live on 2026-09-09. Keyed like the ACCOUNT_USAGE task
-- mirrors, not by QUERY_ID: a run that has not started yet, or that failed
-- before a query was submitted, has none.
CREATE TABLE IF NOT EXISTS information_schema_task_history (
    NAME              TEXT,
    QUERY_ID          TEXT,
    SCHEDULED_TIME    TEXT NOT NULL,
    ATTEMPT_NUMBER    INTEGER NOT NULL,
    QUERY_START_TIME  TEXT,
    COMPLETED_TIME    TEXT,
    STATE             TEXT,
    ERROR_CODE        TEXT,
    ERROR_MESSAGE     TEXT,
    _scope            TEXT    NOT NULL,
    _generation       INTEGER NOT NULL,
    _fetched_at       REAL    NOT NULL,
    PRIMARY KEY (_scope, SCHEDULED_TIME, ATTEMPT_NUMBER)
);
CREATE INDEX IF NOT EXISTS information_schema_task_history_scope
    ON information_schema_task_history(_scope);

-- Every grant on one schema or one database (schema-level and database-level
-- rows share this one physical table, disambiguated by _scope, same shape
-- either way -- SHOW GRANTS ON SCHEMA/DATABASE both return this exact column
-- set). Metadata layer, needs no warehouse. There is no natural single-
-- column key (a grantee can hold several distinct privileges on the same
-- object, and the same privilege/grantee pair can recur across separate
-- grant statements), so the key below is composite. Checked live on
-- 2026-10-24: re-granting a privilege leaves its one row as it was, and
-- re-granting it WITH GRANT OPTION replaces that row, so the key never saw
-- a duplicate (docs/snowflake-behavior.md "SHOW GRANTS").
--
-- Column names verified live on 2026-09-09, and for SHOW GRANTS ON TABLE on
-- 2026-10-14 (docs/snowflake-behavior.md). granted_by_role_type is
-- granted_by's companion, mirroring the owner_role_type column every other
-- SHOW table carries; it reads 'ROLE' on real output.
CREATE TABLE IF NOT EXISTS show_grants (
    created_on           TEXT,
    privilege             TEXT,
    granted_on            TEXT,
    name                  TEXT,
    granted_to            TEXT,
    grantee_name          TEXT,
    grant_option          TEXT,
    granted_by            TEXT,
    granted_by_role_type  TEXT,
    _scope                TEXT    NOT NULL,
    _generation           INTEGER NOT NULL,
    _fetched_at           REAL    NOT NULL,
    PRIMARY KEY (_scope, granted_to, grantee_name, privilege, granted_on, name, granted_by, created_on)
);
CREATE INDEX IF NOT EXISTS show_grants_scope ON show_grants(_scope);

-- Tags applied to one schema or one database (schema-level and database-
-- level rows share this table, disambiguated by _scope). Sourced via a real
-- SELECT against INFORMATION_SCHEMA.TAG_REFERENCES -- needs a running
-- warehouse, degrades blank without one, same as every SELECT-backed table
-- above. Deliberately not SHOW TAGS IN SCHEMA/DATABASE, which lists tag
-- *objects defined inside* that container rather than tags *applied to* it
-- -- the wrong semantics for this feature. Also deliberately not
-- ACCOUNT_USAGE.TAG_REFERENCES: that needs IMPORTED PRIVILEGES on SNOWFLAKE
-- (or ACCOUNTADMIN) and can lag ~2 hours, both unnecessary here since
-- INFORMATION_SCHEMA.TAG_REFERENCES already covers a single object with no
-- extra privilege and near-real-time data. Column names and values verified
-- live on 2026-09-09 (docs/snowflake-behavior.md). TAG_VALUE is part of the
-- key because a multi-value tag (MULTI_VALUE = TRUE) comes back as one row
-- per value; keyed on TAG_NAME alone, each value would overwrite the one
-- before it.
CREATE TABLE IF NOT EXISTS information_schema_tag_references (
    TAG_DATABASE TEXT,
    TAG_SCHEMA   TEXT,
    TAG_NAME     TEXT,
    TAG_VALUE    TEXT,
    DOMAIN       TEXT,
    _scope       TEXT    NOT NULL,
    _generation  INTEGER NOT NULL,
    _fetched_at  REAL    NOT NULL,
    PRIMARY KEY (_scope, TAG_DATABASE, TAG_SCHEMA, TAG_NAME, TAG_VALUE)
);
CREATE INDEX IF NOT EXISTS information_schema_tag_references_scope
    ON information_schema_tag_references(_scope);

-- Tags applied to every column of one table or view (one row per column per
-- tag value -- see information_schema_tag_references for why), scoped per
-- table like information_schema_columns. Sourced via
-- INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS rather than TAG_REFERENCES
-- called once per column -- that function returns every column's tags for
-- one table in a single call, so this scales with tables, not with columns.
-- Same warehouse/degrade-blank story as information_schema_tag_references
-- above. Verified live on 2026-09-14, which is how the function was found
-- to report inherited schema- and database-level tags against every column
-- too; queries.tag_references_all_columns filters those out
-- (docs/snowflake-behavior.md).
CREATE TABLE IF NOT EXISTS information_schema_tag_references_all_columns (
    TAG_DATABASE TEXT,
    TAG_SCHEMA   TEXT,
    TAG_NAME     TEXT,
    TAG_VALUE    TEXT,
    COLUMN_NAME  TEXT,
    _scope       TEXT    NOT NULL,
    _generation  INTEGER NOT NULL,
    _fetched_at  REAL    NOT NULL,
    PRIMARY KEY (_scope, COLUMN_NAME, TAG_DATABASE, TAG_SCHEMA, TAG_NAME, TAG_VALUE)
);
CREATE INDEX IF NOT EXISTS information_schema_tag_references_all_columns_scope
    ON information_schema_tag_references_all_columns(_scope);

-- Warehouses and roles. Unlike the object mirrors above these are small,
-- flat, unpaged lists (see gateway/queries.py), so there is nothing to key
-- beyond the name -- but they are ordinary SHOW mirrors with the same
-- bookkeeping columns, which is why they live here rather than in a third
-- schema script of their own.
CREATE TABLE IF NOT EXISTS show_warehouses (
    name        TEXT NOT NULL PRIMARY KEY,
    comment     TEXT,
    size        TEXT,
    is_default  TEXT,
    _scope      TEXT    NOT NULL,
    _generation INTEGER NOT NULL,
    _fetched_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS show_warehouses_scope ON show_warehouses(_scope);

CREATE TABLE IF NOT EXISTS show_roles (
    name        TEXT NOT NULL PRIMARY KEY,
    comment     TEXT,
    _scope      TEXT    NOT NULL,
    _generation INTEGER NOT NULL,
    _fetched_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS show_roles_scope ON show_roles(_scope);

-- The freshness ledger: one row per separately-refreshable scope. This is
-- what makes a refresh surgical rather than "fetch everything again".
CREATE TABLE IF NOT EXISTS cache_entry (
    scope       TEXT PRIMARY KEY,
    state       TEXT    NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 0,
    fetched_at  REAL,
    -- When the latest pass began, whether it finished, failed or is still
    -- running. A finished pass copies it into fetched_at, so rows age from
    -- the first page asked for rather than the last one received; a failed
    -- pass is retried one TTL after it.
    attempted_at REAL,
    next_cursor TEXT,
    row_count   INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    -- Whether the statement behind the last completed pass hit its own
    -- row cap, so rows exist that were deliberately not fetched. Distinct
    -- from ERROR (nothing arrived) and from COMPLETE (everything did):
    -- the rows here are real and current, there are just more of them
    -- upstream. Added in place, which means an existing cache file has to
    -- be deleted -- see the header above.
    truncated   INTEGER NOT NULL DEFAULT 0,
    -- Only for a log scope (see TableSpec.log): the epoch back to which the
    -- stored rows are complete, set by a full pass. A window pass asks only
    -- for what is newer than the last one, which is safe while this still
    -- reaches as far back as the fetch window. NULL means the next pass
    -- must be a full one.
    covered_from REAL
);
