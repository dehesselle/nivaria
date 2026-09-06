from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import platformdirs

APP_NAME = "snowcone"


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))


def settings_file() -> Path:
    return config_dir() / "settings.json"


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


def write_json_atomic(path: Path, data: Any) -> None:
    """Create the parent dir, write to .tmp, then replace.

    Shared by settings.py and connections.py so a crash or a concurrent read
    never sees a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def read_json(path: Path, default: Any) -> Any:
    """Read JSON, degrading to `default` on anything going wrong.

    Every store in snowcone is an optimisation, never a dependency: an absent,
    unreadable or corrupt file means "no data", not a crash on startup.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
