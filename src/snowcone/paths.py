"""Where snowcone keeps its files.

The generic parts -- per-app config/cache directories and the JSON read/write
helpers -- come from `tcw.paths`; what stays here is the Snowflake-specific
part, above all the per-identity cache file.
"""

from __future__ import annotations

from pathlib import Path

from tcw.paths import cache_dir as _cache_dir
from tcw.paths import config_dir as _config_dir
from tcw.paths import read_json, write_json_atomic
from tcw.paths import settings_file as _settings_file

APP_NAME = "snowcone"

__all__ = [
    "APP_NAME",
    "cache_dir",
    "cache_file",
    "config_dir",
    "credential_cache_file",
    "read_json",
    "recent_file",
    "settings_file",
    "write_json_atomic",
]


def config_dir() -> Path:
    return _config_dir(APP_NAME)


def cache_dir() -> Path:
    return _cache_dir(APP_NAME)


def settings_file() -> Path:
    return _settings_file(APP_NAME)


def recent_file() -> Path:
    return config_dir() / "recent.json"


def credential_cache_file() -> Path:
    return config_dir() / "credential_cache.json"


def cache_file(account: str, role: str | None) -> Path:
    """The sqlite cache file for one (account, role) identity.

    SHOW output is filtered by the session's role, and the mirror tables carry
    no per-role column, so two different roles must never be able to read each
    other's cached rows -- including two different accounts that happen to use
    the same role name, e.g. a generic SYSADMIN. One file per identity makes
    that a non-issue by construction, and switching back to a role already
    used in this session reopens its own warm file rather than an empty one.

    Warehouse is deliberately not part of the key: SHOW is served by
    Snowflake's metadata layer and consults no warehouse, so nothing it
    returns can vary with which one is active.
    """
    return cache_dir() / f"cache-{_slug(account)}-{_slug(role)}.db"


def _slug(value: str | None) -> str:
    """A filesystem-safe stand-in for an identifier that may contain almost
    anything -- Snowflake quoted identifiers allow it."""
    text = value or "default"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in text)
    return safe or "default"
