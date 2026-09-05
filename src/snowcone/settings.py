"""Typed settings with a self-registering declaration.

Declaring a `Setting` at module scope registers it, so a settings dialog can
enumerate everything with no per-setting wiring. `ConfigStore` persists only
values somebody actually overrode -- a setting left at its default stays
absent from the file, so a later release can change that default without a
stale override silently shadowing it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowcone.paths import read_json, settings_file, write_json_atomic

_registry: dict[str, Setting[Any]] = {}


def _parse_bool(text: str) -> bool:
    return text.strip().lower() in {"1", "true", "yes", "on"}


_PARSERS: dict[type, Callable[[str], Any]] = {
    bool: _parse_bool,
    int: int,
    float: float,
    str: str,
}


@dataclass(frozen=True)
class Setting[T: (bool, int, float, str)]:
    key: str
    label: str
    description: str
    group: str
    default: T
    parse: Callable[[str], T] = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.parse is None:
            object.__setattr__(self, "parse", _PARSERS[type(self.default)])
        if self.key in _registry:
            raise ValueError(f"duplicate setting key: {self.key!r}")
        _registry[self.key] = self


def all_settings() -> list[Setting[Any]]:
    return sorted(_registry.values(), key=lambda s: (s.group, s.label))


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_file()
        self._values: dict[str, Any] = {}
        self._loaded = False

    def load(self) -> None:
        data = read_json(self.path, {})
        self._values = data if isinstance(data, dict) else {}
        self._loaded = True

    def get[T: (bool, int, float, str)](self, setting: Setting[T]) -> T:
        if not self._loaded:
            self.load()
        value = self._values.get(setting.key, setting.default)
        # A hand-edited settings.json can hold anything; a wrong type here
        # would surface as a TypeError deep inside a worker instead of at the
        # point of use, so fall back to the default rather than trust it.
        if not isinstance(value, type(setting.default)):
            return setting.default
        return value

    def set[T: (bool, int, float, str)](self, setting: Setting[T], value: T) -> None:
        if not self._loaded:
            self.load()
        if value == setting.default:
            self._values.pop(setting.key, None)
        else:
            self._values[setting.key] = value
        try:
            write_json_atomic(self.path, self._values)
        except OSError:
            pass


#: The single store every module imports by name. Call `config.get(SETTING)`
#: at the point of use rather than caching the result, so a change in the
#: settings dialog takes effect without a restart.
config = ConfigStore()


CACHE_TTL = Setting(
    key="cache_ttl",
    label="cache age",
    description=(
        "How many seconds cached metadata stays fresh before a background "
        "refresh is dispatched for it. 0 disables background refreshing "
        "(and blanks the footer countdown); an explicit refresh still works."
    ),
    group="Cache",
    default=900,
)

POOL_SIZE = Setting(
    key="pool_size",
    label="connection pool size",
    description=(
        "How many persistent Snowflake connections to open at startup. Every "
        "connection may cost an MFA prompt, so they are all opened up front "
        "and then reused for the rest of the session."
    ),
    group="Connection",
    default=3,
)

PAGE_SIZE = Setting(
    key="page_size",
    label="fetch page size",
    description=(
        "How many rows one fetch job asks Snowflake for. Smaller pages make "
        "a cold cache fill the tree in more, smaller visible steps."
    ),
    group="Cache",
    default=1000,
)
