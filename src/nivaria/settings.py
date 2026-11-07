# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""nivaria's settings.

The machinery -- `Setting`, the registry, `ConfigStore`, the dialog -- lives
in `tcoda.settings`. This module is only the declarations, plus the one call
that tells the shared store which app's config file to use. Import `config`
from here or from `tcoda.settings`; it is the same object either way.

Read values at the point of use (`config.get(CACHE_TTL)`) rather than caching
them, so a change in the settings dialog takes effect without a restart.
"""

from __future__ import annotations

from tcoda.settings import Setting, config, configure

from nivaria.paths import APP_NAME

configure(APP_NAME)

__all__ = [
    "CACHE_TTL",
    "CURSOR_SETTLE_MS",
    "DATA_PREVIEW_ROWS",
    "DATA_PREVIEW_TIMEOUT",
    "HISTORY_MAX_ROWS",
    "LAG_MARGIN_FACTOR",
    "MAX_PAGES_PER_PASS",
    "PAGE_SIZE",
    "PIPE_HISTORY_FETCH_DAYS",
    "PIPE_HISTORY_SHOW_DAYS",
    "POOL_SIZE",
    "PRIME_CHUNK_THRESHOLD",
    "ROW_CEILING",
    "SNOWSIGHT_URL",
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
    default=300,
)

CURSOR_SETTLE_MS = Setting(
    key="cursor_settle_ms",
    label="cursor settle delay (milliseconds)",
    description=(
        "How long the tree cursor has to rest on a node before what it shows "
        "is checked and fetched if needed, so that moving through a list "
        "does not run a query for every row it passes. What is cached shows "
        "at once either way. 0 means no wait."
    ),
    group="Cache",
    default=250,
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

SNOWSIGHT_URL = Setting(
    key="snowsight_url",
    label="Snowsight URL",
    description=(
        "Where links open this account in Snowsight, up to and including the "
        "account, e.g. https://app.eu-central-1.privatelink."
        "snowflakecomputing.com/myorg/myacct. Empty uses the public "
        "app.snowflake.com address. A PrivateLink account needs this: which "
        "private Snowsight host works depends on how your network was set "
        "up, which nivaria cannot see."
    ),
    group="Connection",
    default="",
    # Per account, not global: the URL names the account, and one set for a
    # PrivateLink account must not follow you to the next.
    per_instance=True,
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

LAG_MARGIN_FACTOR = Setting(
    key="account_usage_lag_margin",
    label="ACCOUNT_USAGE lag margin (x published lag)",
    description=(
        "How far back each refresh of pipe and task history reaches before "
        "its previous one, as a multiple of the lag Snowflake publishes for "
        "ACCOUNT_USAGE (up to 2 hours for copy history, 45 minutes for task "
        "history). A row Snowflake publishes later than this is not fetched "
        "again until the history is fetched in full."
    ),
    group="Cache",
    default=2,
    choices=(2, 3, 4),
)

DATA_PREVIEW_ROWS = Setting(
    key="data_preview_rows",
    label="data preview rows",
    description=(
        "How many rows a table's or view's Data tab fetches. They are "
        "whichever rows Snowflake returns first, in no particular order: "
        "sorting the whole table to choose them would cost a full scan. "
        "Needs a warehouse."
    ),
    group="Cache",
    default=100,
)

DATA_PREVIEW_TIMEOUT = Setting(
    key="data_preview_timeout",
    label="data preview timeout (seconds)",
    description=(
        "How long a Data tab's query may run before nivaria asks Snowflake "
        "to cancel it. A view's preview runs the view's own query, and the "
        "row limit above does not shorten one that aggregates or sorts. "
        "0 means no limit."
    ),
    group="Cache",
    default=15,
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
        "The most rows nivaria will read out of any single statement before "
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
        "refetched per schema instead of "
        "being used as-is. Raising it keeps big databases on the cheaper "
        "single round trip; lowering it chunks sooner."
    ),
    group="Cache",
    default=10_000,
)
