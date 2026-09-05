from __future__ import annotations

import sqlite3

from snowcone.cache.db import SCHEMA_VERSION, Database


def test_prepare_creates_the_file_and_its_parent_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "cache.db"
    database = Database(path)
    assert database.prepare()
    assert path.exists()


def test_prepare_stamps_the_schema_version(tmp_path) -> None:
    database = Database(tmp_path / "cache.db")
    database.prepare()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_preparing_twice_is_harmless(tmp_path) -> None:
    """Every migration has to be idempotent: a crash between applying one and
    bumping user_version would otherwise leave a file that never opens again."""
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
        "information_schema_columns",
        "cache_entry",
    } <= names
