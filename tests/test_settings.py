from __future__ import annotations

import json

from snowcone.settings import CACHE_TTL, POOL_SIZE, ConfigStore, all_settings


def test_defaults_are_returned_when_nothing_is_stored(tmp_path) -> None:
    store = ConfigStore(tmp_path / "settings.json")
    assert store.get(CACHE_TTL) == CACHE_TTL.default


def test_setting_a_value_persists_it(tmp_path) -> None:
    path = tmp_path / "settings.json"
    store = ConfigStore(path)
    store.set(CACHE_TTL, 60)
    assert ConfigStore(path).get(CACHE_TTL) == 60


def test_a_value_equal_to_the_default_is_not_written(tmp_path) -> None:
    """A setting nobody meaningfully changed stays absent from the file, so a
    future release can change its default without a stale override winning."""
    path = tmp_path / "settings.json"
    store = ConfigStore(path)
    store.set(CACHE_TTL, 60)
    store.set(CACHE_TTL, CACHE_TTL.default)
    assert json.loads(path.read_text()) == {}


def test_a_wrong_type_in_the_file_falls_back_to_the_default(tmp_path) -> None:
    """A hand-edited settings.json must not surface as a TypeError deep inside
    a worker thread hours later."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"cache_ttl": "not a number"}))
    assert ConfigStore(path).get(CACHE_TTL) == CACHE_TTL.default


def test_a_corrupt_file_degrades_to_defaults(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{ not json")
    assert ConfigStore(path).get(POOL_SIZE) == POOL_SIZE.default


def test_declaring_a_setting_registers_it() -> None:
    keys = {setting.key for setting in all_settings()}
    assert {"cache_ttl", "pool_size", "page_size"} <= keys
