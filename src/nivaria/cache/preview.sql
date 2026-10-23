-- Some of one table's rows, for the Table view's Data tab. Applied only to
-- the in-memory MemoryDatabase, never to a cache file: this is table data,
-- possibly personal data, and must not reach the disk. Kept out of
-- schema.sql for that reason, which also means it carries no SCHEMA_VERSION:
-- an in-memory database is built fresh every time.
--
-- Every table has its own columns, so this cannot mirror a Snowflake shape
-- the way schema.sql's tables do. Each row is one JSON object instead, keyed
-- by column name in SELECT * order. Both columns are nivaria's own, hence
-- the underscores.
CREATE TABLE IF NOT EXISTS data_preview (
    _row_number INTEGER NOT NULL,
    _values     TEXT    NOT NULL,
    _scope      TEXT    NOT NULL,
    _generation INTEGER NOT NULL,
    _fetched_at REAL    NOT NULL,
    PRIMARY KEY (_scope, _row_number)
);
CREATE INDEX IF NOT EXISTS data_preview_scope ON data_preview(_scope);
