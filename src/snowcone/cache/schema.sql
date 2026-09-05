-- snowcone cache.
--
-- The bottom layer mirrors Snowflake: one table per SHOW command, with the
-- result columns named exactly as Snowflake returns them. Collapsing the
-- different object kinds into one generic table would invent terminology and
-- lose columns (SHOW PIPES has definition/notification_channel, SHOW TABLES
-- has rows/bytes/cluster_by), so the repetition below is the cheaper price.
--
-- Bookkeeping columns are underscore-prefixed so they can never collide with
-- a Snowflake column name:
--   _scope       which cache_entry row owns this row
--   _generation  the refresh pass that last wrote it (see mark-and-sweep)
--   _fetched_at  when that pass wrote it

CREATE TABLE IF NOT EXISTS show_databases (
    name           TEXT NOT NULL PRIMARY KEY,
    created_on     TEXT,
    kind           TEXT,
    owner          TEXT,
    comment        TEXT,
    retention_time TEXT,
    _scope         TEXT    NOT NULL,
    _generation    INTEGER NOT NULL,
    _fetched_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS show_databases_scope ON show_databases(_scope);

CREATE TABLE IF NOT EXISTS show_schemas (
    database_name  TEXT NOT NULL,
    name           TEXT NOT NULL,
    created_on     TEXT,
    owner          TEXT,
    comment        TEXT,
    retention_time TEXT,
    _scope         TEXT    NOT NULL,
    _generation    INTEGER NOT NULL,
    _fetched_at    REAL    NOT NULL,
    PRIMARY KEY (database_name, name)
);
CREATE INDEX IF NOT EXISTS show_schemas_scope ON show_schemas(_scope);

CREATE TABLE IF NOT EXISTS show_tables (
    database_name  TEXT NOT NULL,
    schema_name    TEXT NOT NULL,
    name           TEXT NOT NULL,
    created_on     TEXT,
    kind           TEXT,
    comment        TEXT,
    cluster_by     TEXT,
    rows           INTEGER,
    bytes          INTEGER,
    owner          TEXT,
    retention_time TEXT,
    _scope         TEXT    NOT NULL,
    _generation    INTEGER NOT NULL,
    _fetched_at    REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_tables_scope ON show_tables(_scope);

CREATE TABLE IF NOT EXISTS show_views (
    database_name TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    name          TEXT NOT NULL,
    created_on    TEXT,
    comment       TEXT,
    owner         TEXT,
    text          TEXT,
    is_secure     TEXT,
    _scope        TEXT    NOT NULL,
    _generation   INTEGER NOT NULL,
    _fetched_at   REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_views_scope ON show_views(_scope);

CREATE TABLE IF NOT EXISTS show_pipes (
    database_name        TEXT NOT NULL,
    schema_name          TEXT NOT NULL,
    name                 TEXT NOT NULL,
    created_on           TEXT,
    definition           TEXT,
    owner                TEXT,
    notification_channel TEXT,
    comment              TEXT,
    pattern              TEXT,
    integration          TEXT,
    _scope               TEXT    NOT NULL,
    _generation          INTEGER NOT NULL,
    _fetched_at          REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_pipes_scope ON show_pipes(_scope);

CREATE TABLE IF NOT EXISTS show_stages (
    database_name TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    name          TEXT NOT NULL,
    created_on    TEXT,
    url           TEXT,
    type          TEXT,
    owner         TEXT,
    comment       TEXT,
    _scope        TEXT    NOT NULL,
    _generation   INTEGER NOT NULL,
    _fetched_at   REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_stages_scope ON show_stages(_scope);

CREATE TABLE IF NOT EXISTS show_tasks (
    database_name TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    name          TEXT NOT NULL,
    created_on    TEXT,
    definition    TEXT,
    owner         TEXT,
    comment       TEXT,
    warehouse     TEXT,
    schedule      TEXT,
    state         TEXT,
    _scope        TEXT    NOT NULL,
    _generation   INTEGER NOT NULL,
    _fetched_at   REAL    NOT NULL,
    PRIMARY KEY (database_name, schema_name, name)
);
CREATE INDEX IF NOT EXISTS show_tasks_scope ON show_tasks(_scope);

-- INFORMATION_SCHEMA.COLUMNS, with its own upper-case names preserved.
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
    COMMENT                  TEXT,
    _scope                   TEXT    NOT NULL,
    _generation              INTEGER NOT NULL,
    _fetched_at              REAL    NOT NULL,
    PRIMARY KEY (TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME)
);
CREATE INDEX IF NOT EXISTS information_schema_columns_scope
    ON information_schema_columns(_scope);

-- The freshness ledger: one row per separately-refreshable scope. This is
-- what makes a refresh surgical rather than "fetch everything again".
CREATE TABLE IF NOT EXISTS cache_entry (
    scope       TEXT PRIMARY KEY,
    state       TEXT    NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 0,
    started_at  REAL,
    fetched_at  REAL,
    next_cursor TEXT,
    row_count   INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
