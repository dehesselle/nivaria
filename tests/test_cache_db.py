from __future__ import annotations

import hashlib
import sqlite3

import pytest

from nivaria.cache.db import _SCHEMA_SCRIPTS, SCHEMA_VERSION, Database, MemoryDatabase

#: The SHA-256 of the schema scripts as of SCHEMA_VERSION 3. Change it only
#: together with a bump of SCHEMA_VERSION.
SCHEMA_DIGEST = "99122ad5b688dad324f2c1d091cbc5c84193f7623e24dbee8e9d314ed884dad7"


def test_prepare_creates_the_file_and_its_parent_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "cache.db"
    database = Database(path)
    assert database.prepare()
    assert path.exists()


def test_preparing_twice_is_harmless(tmp_path) -> None:
    """Every schema script is CREATE ... IF NOT EXISTS, so applying them again
    on a reopened file must not fail or disturb what is already there."""
    path = tmp_path / "cache.db"
    assert Database(path).prepare()
    assert Database(path).prepare()


def test_existing_rows_survive_a_reopen(tmp_path) -> None:
    path = tmp_path / "cache.db"
    first = Database(path)
    first.prepare()
    with first.connect() as connection:
        connection.execute(
            "INSERT INTO cache_entry (scope, state, generation) VALUES (?, ?, ?)",
            ("databases", "complete", 1),
        )
    second = Database(path)
    second.prepare()
    with second.connect() as connection:
        row = connection.execute("SELECT * FROM cache_entry").fetchone()
    assert row["scope"] == "databases"


def test_a_fresh_cache_is_stamped_with_the_current_schema_version(tmp_path) -> None:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    with database.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_a_cache_from_another_schema_version_is_replaced_not_migrated(
    tmp_path,
) -> None:
    path = tmp_path / "cache.db"
    old = Database(path)
    old.prepare()
    with old.connect() as connection:
        connection.execute(
            "INSERT INTO cache_entry (scope, state, generation) VALUES (?, ?, ?)",
            ("databases", "complete", 1),
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")

    new = Database(path)
    assert new.prepare()
    with new.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cache_entry").fetchone()[0] == 0
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_a_schema_change_comes_with_a_version_bump() -> None:
    """IF NOT EXISTS keeps an edited script from ever reaching an existing
    cache file; only a SCHEMA_VERSION bump replaces it. Editing schema.sql or
    search.sql therefore fails here until both the version and the digest
    above are updated."""
    digest = hashlib.sha256("".join(_SCHEMA_SCRIPTS).encode()).hexdigest()
    assert (SCHEMA_VERSION, digest) == (3, SCHEMA_DIGEST), (
        "the schema scripts changed: bump SCHEMA_VERSION in cache/db.py, then "
        "update the version and SCHEMA_DIGEST in this test"
    )


def test_wal_is_enabled_so_a_writer_never_blocks_the_ui(tmp_path) -> None:
    """A fetch worker writing a page must not stall the event loop reading the
    rows already cached."""
    database = Database(tmp_path / "cache.db")
    database.prepare()
    with database.connect() as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_an_unusable_path_degrades_instead_of_raising(tmp_path) -> None:
    """If the cache cannot be opened the app still has to start; it just has
    nothing cached."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    database = Database(blocker / "cache.db")
    assert database.prepare() is False
    assert database.usable is False


def test_a_failed_statement_rolls_back(tmp_path) -> None:
    database = Database(tmp_path / "cache.db")
    database.prepare()
    try:
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO cache_entry (scope, state, generation) VALUES (?, ?, ?)",
                ("databases", "complete", 1),
            )
            connection.execute("SELECT * FROM no_such_table")
    except sqlite3.Error:
        pass
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cache_entry").fetchone()[0] == 0


def test_every_mirror_table_exists(tmp_path) -> None:
    database = Database(tmp_path / "cache.db")
    database.prepare()
    with database.connect() as connection:
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "show_databases",
        "show_schemas",
        "show_tables",
        "show_views",
        "show_pipes",
        "show_stages",
        "show_tasks",
        "show_warehouses",
        "show_roles",
        "information_schema_columns",
        "information_schema_tables",
        "account_usage_copy_history",
        "information_schema_copy_history",
        "cache_entry",
    } <= names


def test_a_memory_database_rolls_back_a_failed_unit_of_work() -> None:
    """Its one connection outlives every unit of work, so a failure must not
    leave half a write behind for the next one to commit."""

    database = MemoryDatabase()
    with pytest.raises(RuntimeError), database.connect() as connection:
        connection.execute(
            "INSERT INTO cache_entry (scope, state, generation) VALUES (?, ?, ?)",
            ("data_preview:MYDB.PUBLIC.ORDERS", "partial", 1),
        )
        raise RuntimeError("worker died")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cache_entry").fetchone()[0] == 0


def test_a_memory_database_keeps_its_rows_between_units_of_work() -> None:
    """The reason it holds its connection: a closed in-memory connection
    takes its database with it."""

    database = MemoryDatabase()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO data_preview (_row_number, _values, _scope, _generation,"
            " _fetched_at) VALUES (0, '{}', 'data_preview:MYDB.PUBLIC.ORDERS', 1, 0)"
        )
    with database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM data_preview").fetchone()[0] == 1
        )
