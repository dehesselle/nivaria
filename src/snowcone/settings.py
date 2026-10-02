"""snowcone's settings.

The machinery -- `Setting`, the registry, `ConfigStore`, the dialog -- lives
in `tcoda.settings`. This module is only the declarations, plus the one call
that tells the shared store which app's config file to use. Import `config`
from here or from `tcoda.settings`; it is the same object either way.

Read values at the point of use (`config.get(CACHE_TTL)`) rather than caching
them, so a change in the settings dialog takes effect without a restart.
"""

from __future__ import annotations

from tcoda.settings import Setting, config, configure

from snowcone.paths import APP_NAME

configure(APP_NAME)

__all__ = [
    "CACHE_TTL",
    "HISTORY_MAX_ROWS",
    "MAX_PAGES_PER_PASS",
    "PAGE_SIZE",
    "PIPE_HISTORY_FETCH_DAYS",
    "PIPE_HISTORY_SHOW_DAYS",
    "POOL_SIZE",
    "PRIME_CHUNK_THRESHOLD",
    "ROW_CEILING",
    "TASK_HISTORY_FETCH_DAYS",
    "TASK_HISTORY_SHOW_DAYS",
    "Setting",
    "config",
]


CACHE_TTL = Setting(
    key="cache_ttl",
    label="cache age",
    description=(
        "How many seconds cached metadata stays fresh before a background "
        "refresh is dispatched for it. 0 disables background refreshing "
        "(and hides the footer countdown); an explicit refresh still works."
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
    choices=(1, 2, 3, 4, 5),
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

PIPE_HISTORY_FETCH_DAYS = Setting(
    key="pipe_history_fetch_days",
    label="pipe history fetch window (days)",
    description=(
        "How many days of pipe load history to pull from "
        "SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY per schema, kept in the cache "
        "regardless of how much of it the pipe detail view actually shows -- "
        "see 'pipe history display window' below."
    ),
    group="Cache",
    default=90,
)

PIPE_HISTORY_SHOW_DAYS = Setting(
    key="pipe_history_show_days",
    label="pipe history display window (days)",
    description=(
        "How many days of a pipe's load history to show in its detail view. "
        "Can be smaller than the fetch window above to keep the list short "
        "while still keeping older rows cached for later."
    ),
    group="Cache",
    default=90,
)

TASK_HISTORY_FETCH_DAYS = Setting(
    key="task_history_fetch_days",
    label="task history fetch window (days)",
    description=(
        "How many days of task run history to pull from "
        "SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY per schema, kept in the cache "
        "regardless of how much of it the task detail view actually shows -- "
        "see 'task history display window' below."
    ),
    group="Cache",
    default=90,
)

TASK_HISTORY_SHOW_DAYS = Setting(
    key="task_history_show_days",
    label="task history display window (days)",
    description=(
        "How many days of a task's run history to show in its detail view. "
        "Can be smaller than the fetch window above to keep the list short "
        "while still keeping older rows cached for later."
    ),
    group="Cache",
    default=90,
)

HISTORY_MAX_ROWS = Setting(
    key="history_max_rows",
    label="history row cap",
    description=(
        "The most rows any one pipe- or task-history statement may return. "
        "The newest rows are kept: each history query orders by its own "
        "timestamp descending before applying this. A busy pipe can otherwise "
        "have tens of thousands of loads inside the fetch window above, and "
        "nothing in COPY_HISTORY bounds that on its own."
    ),
    group="Cache",
    default=5000,
)

MAX_PAGES_PER_PASS = Setting(
    key="max_pages_per_pass",
    label="max pages per refresh",
    description=(
        "How many pages one refresh pass may fetch before it gives up and "
        "reports an error. A safety net, not a tuning knob: a SHOW that "
        "silently ignores LIMIT would otherwise let the continuation chain "
        "re-fetch the same page forever."
    ),
    group="Cache",
    default=50,
)

ROW_CEILING = Setting(
    key="row_ceiling",
    label="result row ceiling",
    description=(
        "The most rows snowcone will read out of any single statement before "
        "abandoning it with an error. The last line of defence against a "
        "query that returns far more than expected; it is meant never to be "
        "reached. 0 disables it."
    ),
    group="Cache",
    default=200_000,
)

PRIME_CHUNK_THRESHOLD = Setting(
    key="prime_chunk_threshold",
    label="prime chunk threshold",
    description=(
        "How large a whole-database prime result has to be before it is "
        "refetched in smaller chunks (per schema, then per table) instead of "
        "being used as-is. Raising it keeps big databases on the cheaper "
        "single round trip; lowering it chunks sooner."
    ),
    group="Cache",
    default=10_000,
)
