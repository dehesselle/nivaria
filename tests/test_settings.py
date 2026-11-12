# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""nivaria's own settings declarations.

The registry, the store and the dialog are `tcoda`'s and are tested there; what
matters here is that nivaria declares what it says it declares, and that the
store is pointed at nivaria's config file.
"""

from __future__ import annotations

from tcoda.paths import settings_file as tcoda_settings_file
from tcoda.settings import all_settings, config, configure

from nivaria.paths import APP_NAME
from nivaria.settings import (
    CACHE_TTL,
    CSV_DELIMITER,
    JOURNAL_FILE,
    JOURNAL_VERBOSE,
    PAGE_SIZE,
    PIPE_HISTORY_FETCH_DAYS,
    PIPE_HISTORY_SHOW_DAYS,
    POOL_SIZE,
    SNOWSIGHT_URL,
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
    assert CACHE_TTL.default == 300
    assert POOL_SIZE.default == 3
    assert PAGE_SIZE.default == 1000
    assert PIPE_HISTORY_FETCH_DAYS.default == 90
    assert PIPE_HISTORY_SHOW_DAYS.default == 90


def test_the_csv_delimiter_follows_the_operating_system_unless_told_otherwise() -> None:
    assert CSV_DELIMITER.group == "Export"
    assert CSV_DELIMITER.default == "operating system"
    assert CSV_DELIMITER.choices == ("operating system", "comma", "semicolon")


def test_nothing_about_what_the_user_did_reaches_the_disk_unasked() -> None:
    """The journal is always in the Log panel, in memory; its file is opt-in."""
    assert (JOURNAL_FILE.group, JOURNAL_VERBOSE.group) == ("Diagnostics", "Diagnostics")
    assert JOURNAL_FILE.default is False
    assert JOURNAL_VERBOSE.default is False


def test_pool_size_is_limited_to_a_fixed_set_of_choices() -> None:
    """Rendered as a dropdown in the dialog rather than free text -- see
    tcoda's SettingsDialog._make_value_widget."""
    assert POOL_SIZE.choices == (1, 2, 3, 4, 5)


def test_a_ttl_of_zero_round_trips() -> None:
    """0 is the value that disables the staleness sweep, and it is not the
    default, so it must actually reach the file rather than being pruned."""
    config.set(CACHE_TTL, 0)
    assert config.get(CACHE_TTL) == 0


def test_the_store_is_pointed_at_nivarias_own_config_file() -> None:
    """The conftest fixture re-points the store at tmp_path, so this asks the
    same question the other way round: configuring for nivaria lands on
    nivaria's config file. Resolving a path touches nothing on disk, and the
    fixture restores isolation afterwards.
    """
    configure(APP_NAME)
    assert config.path == tcoda_settings_file(APP_NAME)
    assert config.path.parent.name == APP_NAME


def test_the_snowsight_url_is_kept_per_account() -> None:
    """One set for a PrivateLink account must not follow you to the next."""
    assert SNOWSIGHT_URL.per_instance
    config.current_instance = "myorg-one"
    config.set(SNOWSIGHT_URL, "https://one.example")
    config.current_instance = "myorg-two"
    assert config.get(SNOWSIGHT_URL) == ""
