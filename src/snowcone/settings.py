"""snowcone's settings.

The machinery -- `Setting`, the registry, `ConfigStore`, the dialog -- lives
in `tcw.settings`. This module is only the declarations, plus the one call
that tells the shared store which app's config file to use. Import `config`
from here or from `tcw.settings`; it is the same object either way.

Read values at the point of use (`config.get(CACHE_TTL)`) rather than caching
them, so a change in the settings dialog takes effect without a restart.
"""

from __future__ import annotations

from tcw.settings import Setting, config, configure

from snowcone.paths import APP_NAME

configure(APP_NAME)

__all__ = ["CACHE_TTL", "PAGE_SIZE", "POOL_SIZE", "Setting", "config"]


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
        "How many persistent Snowflake connections to open at startup. The "
        "first is opened as soon as you connect; every connection may cost an "
        "MFA prompt, so anything beyond that first one is only opened after "
        "you confirm it in the connect dialog. All of them are then reused "
        "for the rest of the session."
    ),
    group="Connection",
    default=3,
)

PAGE_SIZE = Setting(
    key="page_size",
    label="fetch page size",
    description=(
        "How many rows one fetch job asks Snowflake for. Bigger is faster: a "
        "SHOW round trip costs about the same regardless of how many rows it "
        "returns, so pages exist to bound a job, not to make progress "
        "visible. Lowering this mainly multiplies latency."
    ),
    group="Cache",
    default=1000,
)
