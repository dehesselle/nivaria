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


def cache_file() -> Path:
    return cache_dir() / "cache.db"


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
