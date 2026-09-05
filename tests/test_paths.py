from __future__ import annotations

from snowcone.paths import read_json, write_json_atomic


def test_write_json_atomic_creates_parent_dirs(tmp_path) -> None:
    path = tmp_path / "deep" / "nested" / "data.json"
    write_json_atomic(path, {"a": 1})
    assert read_json(path, None) == {"a": 1}


def test_write_json_atomic_leaves_no_tmp_file_behind(tmp_path) -> None:
    path = tmp_path / "data.json"
    write_json_atomic(path, [1, 2])
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_read_json_returns_the_default_for_a_missing_file(tmp_path) -> None:
    assert read_json(tmp_path / "absent.json", []) == []


def test_read_json_returns_the_default_for_corrupt_content(tmp_path) -> None:
    """Every store here is an optimisation, never a dependency -- a corrupt
    file means 'no data', not a crash before the UI even opens."""
    path = tmp_path / "data.json"
    path.write_text("{ not json")
    assert read_json(path, {"fallback": True}) == {"fallback": True}
