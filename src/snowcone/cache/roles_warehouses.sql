-- Warehouses and roles: unlike the object mirrors in schema.sql, these are
-- small, flat, unpaged lists (see gateway/queries.py), so there is nothing to
-- key beyond the name.

CREATE TABLE IF NOT EXISTS show_warehouses (
    name        TEXT NOT NULL PRIMARY KEY,
    comment     TEXT,
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
