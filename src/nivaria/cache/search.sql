-- A uniform view over every searchable object, whatever kind it is.
--
-- A view rather than a maintained index table: the mirror tables are the only
-- source of truth, so a denormalised copy would need an ingest hook and could
-- drift out of step with them. Scanning it costs a single query whose result
-- is then ranked in Python, which is fast enough that a keystroke never waits.

CREATE VIEW IF NOT EXISTS search_object AS
    SELECT database_name, schema_name, name, 'TABLE' AS kind FROM show_tables
    UNION ALL
    SELECT database_name, schema_name, name, 'VIEW'  AS kind FROM show_views
    UNION ALL
    SELECT database_name, schema_name, name, 'PIPE'  AS kind FROM show_pipes
    UNION ALL
    SELECT database_name, schema_name, name, 'STAGE' AS kind FROM show_stages
    UNION ALL
    SELECT database_name, schema_name, name, 'TASK'  AS kind FROM show_tasks;
