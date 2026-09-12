"""snowcone's own settings declarations.

The registry, the store and the dialog are `tcw`'s and are tested there; what
matters here is that snowcone declares what it says it declares, and that the
store is pointed at snowcone's config file.
"""

from __future__ import annotations

from tcw.paths import settings_file as tcw_settings_file
from tcw.settings import all_settings, config, configure

from snowcone.paths import APP_NAME
from snowcone.settings import (
    CACHE_TTL,
    PAGE_SIZE,
    PIPE_HISTORY_FETCH_DAYS,
    PIPE_HISTORY_SHOW_DAYS,
    POOL_SIZE,
)


def test_the_settings_are_registered() -> None:
    keys = {entry.key for entry in all_settings()}
    assert {
        "cache_ttl",
        "pool_size",
        "page_size",
        "pipe_history_fetch_days",
        "pipe_history_show_days",
    } <= keys


def test_they_are_grouped_the_way_the_dialog_renders_them() -> None:
    assert (CACHE_TTL.group, PAGE_SIZE.group) == ("Cache", "Cache")
    assert POOL_SIZE.group == "Connection"
    assert (PIPE_HISTORY_FETCH_DAYS.group, PIPE_HISTORY_SHOW_DAYS.group) == (
        "Cache",
        "Cache",
    )


def test_their_defaults_are_the_documented_ones() -> None:
    assert CACHE_TTL.default == 900
    assert POOL_SIZE.default == 3
    assert PAGE_SIZE.default == 1000
    assert PIPE_HISTORY_FETCH_DAYS.default == 90
    assert PIPE_HISTORY_SHOW_DAYS.default == 90


def test_pool_size_is_limited_to_a_fixed_set_of_choices() -> None:
    """Rendered as a dropdown in the dialog rather than free text -- see
    tcw's SettingsDialog._make_value_widget."""
    assert POOL_SIZE.choices == (1, 2, 3, 4, 5)


def test_a_ttl_of_zero_round_trips() -> None:
    """0 is the value that disables the staleness sweep, and it is not the
    default, so it must actually reach the file rather than being pruned."""
    config.set(CACHE_TTL, 0)
    assert config.get(CACHE_TTL) == 0


def test_the_store_is_pointed_at_snowcones_own_config_file() -> None:
    """The conftest fixture re-points the store at tmp_path, so this asks the
    same question the other way round: configuring for snowcone lands on
    snowcone's config file. Resolving a path touches nothing on disk, and the
    fixture restores isolation afterwards.
    """
    configure(APP_NAME)
    assert config.path == tcw_settings_file(APP_NAME)
    assert config.path.parent.name == APP_NAME
