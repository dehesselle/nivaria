"""Fetch job definitions and the page-fetch dispatch.

**One job fetches exactly one page.** If more pages remain, the job re-enqueues
its own continuation at the same priority rather than looping internally. That
single decision buys four properties at once:

* the tree grows visibly after every page, so a cold cache is never a frozen UI
* a user expanding a node overtakes a long background sweep at the next page
  boundary, instead of queueing behind the whole scope
* no job can hold a pool connection for longer than one page
* cancellation is free -- a superseded job simply does not re-enqueue, which
  matters because a thread worker's blocking network call cannot be interrupted
"""

from __future__ import annotations

import datetime
import itertools
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from typing import Any

from nivaria.gateway.gateway import SnowflakeGateway
from nivaria.gateway.queries import MAX_PAGE_SIZE
from nivaria.models import Scope, ScopeType
from nivaria.settings import (
    DATA_PREVIEW_ROWS,
    DATA_PREVIEW_TIMEOUT,
    HISTORY_MAX_ROWS,
    PIPE_HISTORY_FETCH_DAYS,
    PRIME_CHUNK_THRESHOLD,
    TASK_HISTORY_FETCH_DAYS,
    config,
)

_sequence = itertools.count()


class Priority(IntEnum):
    """Lower sorts first out of the queue."""

    #: The user is waiting: they just expanded a node or asked for a refresh.
    INTERACTIVE = 0
    #: On screen but not just asked for.
    VISIBLE = 1
    #: A TTL sweep. Always yields to the two above at a page boundary.
    BACKGROUND = 2


@dataclass(frozen=True, order=True)
class FetchJob:
    """One page of one scope.

    Ordering is (priority, seq) so the queue is FIFO within a priority tier;
    everything else is excluded from comparison because it plays no part in
    scheduling and Scope/None would otherwise have to be mutually orderable.
    """

    priority: Priority
    seq: int = field(default_factory=lambda: next(_sequence))
    scope: Scope = field(compare=False, default=None)  # type: ignore[assignment]
    cursor: str | None = field(compare=False, default=None)
    #: The stop sentinel. It rides the same queue as real work, because a
    #: PriorityQueue has to be able to order everything in it -- a bare
    #: object() sentinel raises as soon as the queue is not empty.
    shutdown: bool = field(compare=False, default=False)
    #: 0 until the first page claims the pass; carried by continuations so
    #: every page of one pass writes under the same generation stamp.
    generation: int = field(compare=False, default=0)
    #: Which page of this pass this job is, counted from 0. Only the
    #: scheduler's page budget reads it -- see FetchService._execute.
    page: int = field(compare=False, default=0)

    def next_page(self, cursor: str, generation: int) -> FetchJob:
        return FetchJob(
            priority=self.priority,
            seq=next(_sequence),
            scope=self.scope,
            cursor=cursor,
            generation=generation,
            page=self.page + 1,
        )


#: The column a SHOW result is keyed and paged by.
NAME_COLUMN = "name"


@dataclass(frozen=True)
class Page:
    """One page of one scope, and what the caller needs to know about it.

    `cursor` is the keyset cursor for the next page, or None when this was
    the last one. `truncated` says the statement hit its own row cap, so
    rows exist that were deliberately not fetched -- a different thing from
    a short page, and the one the UI has to be able to admit to.
    """

    rows: list[dict[str, Any]]
    cursor: str | None = None
    truncated: bool = False


def fetch_page(
    gateway: SnowflakeGateway,
    scope: Scope,
    cursor: str | None,
    limit: int,
    *,
    allow_reconnect: bool = True,
    pipe_target: tuple[str, str, str] | None = None,
) -> Page:
    """Fetch one page.

    `Page.cursor` is the last row's name when the page came back full, and
    None when it did not -- a short page means the end of the result, which is
    what terminates the continuation chain. It is also None for a statement
    that cannot be paged at all (`ObjectType.pageable`), where one call is
    the whole result and asking for more would re-fetch it forever.

    `allow_reconnect` is passed straight through to the gateway: false for a
    background-priority job, so a dead pooled connection is never repaired by
    opening a new, possibly-interactive session while nobody is watching --
    see `ConnectionPool.acquire`.

    `pipe_target` is only meaningful for ScopeType.PIPE_HISTORY, whose query
    needs the pipe's target table (catalog, schema, table) -- a piece of
    context this function has no way to resolve itself (it only has a
    gateway, not the repository, so it cannot look up cached data). The
    caller (`FetchService._execute`) resolves it before calling here; every
    other scope type ignores this parameter.

    A continuation page asks for one row more than `limit`, because `SHOW
    TASKS ... FROM 'name'` is inclusive: it starts with the cursor row itself,
    where every other pageable SHOW starts after it. That echoed row is
    dropped, and the spare row keeps a one-row page size moving forward.
    Without it, the echo alone would fill the page and look like a stuck
    cursor. An exclusive SHOW fills the spare row with a new one, which is
    kept: trimming it would also trim a statement that ignores LIMIT, whose
    page the stuck-cursor guard then takes for the whole result.
    """
    asked = limit if cursor is None else min(limit + 1, MAX_PAGE_SIZE)
    match scope.type:
        case ScopeType.DATABASES:
            rows = gateway.show_databases(
                after=cursor, limit=asked, allow_reconnect=allow_reconnect
            )
        case ScopeType.SCHEMAS:
            assert scope.database is not None
            rows = gateway.show_schemas(
                scope.database,
                after=cursor,
                limit=asked,
                allow_reconnect=allow_reconnect,
            )
        case ScopeType.OBJECTS:
            assert scope.object_type is not None
            assert scope.database is not None
            assert scope.schema is not None
            if not scope.object_type.pageable:
                # SHOW PIPES/STAGES ignore LIMIT/FROM, so one call is the
                # whole schema and there is no second page to ask for --
                # asking anyway is what used to loop forever. Nothing can
                # detect truncation here either: without a working LIMIT
                # there is no row count that means 'there was more'. The
                # row ceiling in PoolGateway is the only bound left.
                return Page(
                    gateway.show_objects(
                        scope.object_type,
                        scope.database,
                        scope.schema,
                        allow_reconnect=allow_reconnect,
                    )
                )
            rows = gateway.show_objects(
                scope.object_type,
                scope.database,
                scope.schema,
                after=cursor,
                limit=asked,
                allow_reconnect=allow_reconnect,
            )
        case ScopeType.COLUMNS:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None
            # A single table's columns are a small, bounded result, so this
            # one is deliberately not paged.
            return Page(
                gateway.show_columns(
                    scope.database,
                    scope.schema,
                    scope.table,
                    allow_reconnect=allow_reconnect,
                )
            )
        case ScopeType.TABLE_STATS:
            assert scope.database is not None
            assert scope.schema is not None
            # A schema's table count is bounded, same reasoning as
            # show_columns being deliberately unpaged.
            return Page(
                gateway.table_stats(
                    scope.database, scope.schema, allow_reconnect=allow_reconnect
                ),
            )
        case ScopeType.VIEW_STATS:
            assert scope.database is not None
            assert scope.schema is not None
            # A schema's view count is bounded, same reasoning as
            # TABLE_STATS being deliberately unpaged.
            return Page(
                gateway.view_stats(
                    scope.database, scope.schema, allow_reconnect=allow_reconnect
                ),
            )
        case ScopeType.MATERIALIZED_VIEW_STATS:
            assert scope.database is not None
            assert scope.schema is not None
            # A schema's materialized-view count is bounded, same reasoning
            # as TABLE_STATS/VIEW_STATS being deliberately unpaged.
            return Page(
                gateway.materialized_view_stats(
                    scope.database, scope.schema, allow_reconnect=allow_reconnect
                ),
            )
        case ScopeType.PIPE_ACTIVITY:
            assert scope.database is not None
            assert scope.schema is not None
            # Bounded by pipe count, not by history depth -- see
            # queries.pipe_activity's QUALIFY.
            fetch_days = config.get(PIPE_HISTORY_FETCH_DAYS)
            return Page(
                gateway.pipe_activity(
                    scope.database,
                    scope.schema,
                    fetch_days,
                    allow_reconnect=allow_reconnect,
                ),
            )
        case ScopeType.PIPE_ACTIVITY_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None  # the pipe's name
            # Bounded to one pipe's history -- no target-table resolution
            # needed, unlike PIPE_HISTORY, since ACCOUNT_USAGE.COPY_HISTORY
            # has an ordinary PIPE_NAME column to filter on directly.
            fetch_days = config.get(PIPE_HISTORY_FETCH_DAYS)
            max_rows = config.get(HISTORY_MAX_ROWS)
            return _capped(
                gateway.pipe_activity_history(
                    scope.database,
                    scope.schema,
                    scope.table,
                    fetch_days,
                    max_rows,
                    allow_reconnect=allow_reconnect,
                ),
                max_rows,
            )
        case ScopeType.PIPE_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None  # the pipe's name
            if pipe_target is None:
                raise ValueError(
                    f"cannot determine the target table for pipe {scope.table} "
                    "(no cached pipe-activity data to resolve it from yet)"
                )
            target_catalog, target_schema, target_table = pipe_target
            max_rows = config.get(HISTORY_MAX_ROWS)
            return _capped(
                gateway.pipe_history(
                    target_catalog,
                    target_schema,
                    target_table,
                    scope.table,
                    max_rows,
                    allow_reconnect=allow_reconnect,
                ),
                max_rows,
            )
        case ScopeType.TASK_ACTIVITY:
            assert scope.database is not None
            assert scope.schema is not None
            # Bounded by task count, not by history depth -- see
            # queries.task_activity's QUALIFY.
            fetch_days = config.get(TASK_HISTORY_FETCH_DAYS)
            return Page(
                gateway.task_activity(
                    scope.database,
                    scope.schema,
                    fetch_days,
                    allow_reconnect=allow_reconnect,
                ),
            )
        case ScopeType.TASK_ACTIVITY_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None  # the task's name
            # Bounded to one task's history -- no target-table resolution
            # needed, unlike PIPE_HISTORY: ACCOUNT_USAGE.TASK_HISTORY has an
            # ordinary NAME column to filter on directly.
            fetch_days = config.get(TASK_HISTORY_FETCH_DAYS)
            max_rows = config.get(HISTORY_MAX_ROWS)
            return _capped(
                gateway.task_activity_history(
                    scope.database,
                    scope.schema,
                    scope.table,
                    fetch_days,
                    max_rows,
                    allow_reconnect=allow_reconnect,
                ),
                max_rows,
            )
        case ScopeType.TASK_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None  # the task's name
            # Unlike PIPE_HISTORY, TASK_NAME/DATABASE_NAME/SCHEMA_NAME are
            # direct named arguments a task's own Scope already carries, so
            # no resolved target parameter is needed here at all.
            max_rows = config.get(HISTORY_MAX_ROWS)
            return _capped(
                gateway.task_history(
                    scope.database,
                    scope.schema,
                    scope.table,
                    max_rows,
                    allow_reconnect=allow_reconnect,
                ),
                max_rows,
            )
        case ScopeType.GRANTS:
            assert scope.database is not None
            # A schema or database's grant list is bounded, same reasoning
            # as show_columns/table_stats being deliberately unpaged.
            if scope.table is not None:
                assert scope.schema is not None
                assert scope.object_type is not None
                return Page(
                    gateway.show_grants_on_object(
                        scope.object_type,
                        scope.database,
                        scope.schema,
                        scope.table,
                        allow_reconnect=allow_reconnect,
                    ),
                )
            if scope.schema is not None:
                return Page(
                    gateway.show_grants_on_schema(
                        scope.database, scope.schema, allow_reconnect=allow_reconnect
                    ),
                )
            return Page(
                gateway.show_grants_on_database(
                    scope.database, allow_reconnect=allow_reconnect
                ),
            )
        case ScopeType.TAG_REFERENCES:
            assert scope.database is not None
            if scope.schema is not None:
                return Page(
                    gateway.tag_references_schema(
                        scope.database, scope.schema, allow_reconnect=allow_reconnect
                    ),
                )
            return Page(
                gateway.tag_references_database(
                    scope.database, allow_reconnect=allow_reconnect
                ),
            )
        case ScopeType.COLUMN_TAG_REFERENCES:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None
            return Page(
                gateway.tag_references_all_columns(
                    scope.database,
                    scope.schema,
                    scope.table,
                    allow_reconnect=allow_reconnect,
                ),
            )
        case ScopeType.DATA_PREVIEW:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None
            max_rows = config.get(DATA_PREVIEW_ROWS)
            rows = gateway.select_data_preview(
                scope.database,
                scope.schema,
                scope.table,
                max_rows,
                config.get(DATA_PREVIEW_TIMEOUT),
                allow_reconnect=allow_reconnect,
            )
            return _capped(encode_data_preview(rows), max_rows)
        case ScopeType.WAREHOUSES:
            # Small, flat, unpaged lists -- see queries.show_warehouses.
            return Page(gateway.show_warehouses(allow_reconnect=allow_reconnect))
        case ScopeType.ROLES:
            return Page(gateway.show_roles(allow_reconnect=allow_reconnect))
        case ScopeType.DATABASE_PRIME:
            raise ValueError(
                "a prime scope is fetched by fetch_database, not fetch_page"
            )

    full = len(rows) >= asked
    if cursor is not None and rows and str(rows[0][NAME_COLUMN]) == cursor:
        rows = rows[1:]
    if not full or not rows:
        return Page(rows)
    return Page(rows, str(rows[-1][NAME_COLUMN]))


def _capped(rows: list[dict[str, Any]], max_rows: int) -> Page:
    """A single-call result that carried its own row cap.

    A full result is the only signal there is that rows were left behind:
    the statement asked for `max_rows` and got exactly that many, so there
    is no way to tell 'exactly this many exist' from 'more exist'. Erring
    towards claiming truncation is the safe direction -- overstating it
    shows a note about a history that happens to be exactly the cap's
    length; understating it silently presents a partial history as whole.
    """
    return Page(rows, truncated=len(rows) >= max_rows)


def encode_data_preview(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold each table row into the generic `data_preview` mirror shape.

    Every table has its own columns, so there is no mirror table to map them
    onto. Each row becomes one JSON object instead, and a dict keeps
    `SELECT *`'s column order, which the Data tab's header is read from.
    """
    return [
        {
            "_row_number": number,
            "_values": json.dumps(row, default=_jsonable, ensure_ascii=False),
        }
        for number, row in enumerate(rows)
    ]


def _jsonable(value: Any) -> Any:
    """What the driver returns that JSON has no type for.

    A Decimal becomes a string rather than a float, which would lose a
    NUMBER(38,x)'s digits. It is written fixed-point when it has a
    fractional part: `str()` spells -0.0000000001 as `-1E-10`, which is not
    how a NUMBER(38,10) reads anywhere else. Only a positive exponent, which
    a DECFLOAT such as 1.2E+100 has, keeps the E-notation Snowflake shows
    too. Bytes become upper-case hex, Snowflake's own default
    BINARY_OUTPUT_FORMAT.
    """
    if isinstance(value, Decimal):
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent <= 0:
            return format(value, "f")
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex().upper()
    return str(value)


#: Pushed once per worker to end its loop. seq=-1 with the lowest priority
#: value sorts it ahead of every real job, so stopping is prompt rather than
#: queued behind whatever work is outstanding.
SHUTDOWN = FetchJob(priority=Priority.INTERACTIVE, seq=-1, shutdown=True)


def fetch_database(
    gateway: SnowflakeGateway,
    scope: Scope,
    *,
    allow_reconnect: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Every object of one type in a database. Returns (rows, usable).

    Deliberately a single unpaged statement, unlike every other fetch here.

    Keyset paging is unsafe across a whole database: `LIMIT n FROM 'name'`
    resumes after an object *name*, and names are not unique across schemas --
    an ORDERS table in forty schemas is forty rows sharing one cursor value.
    Resuming after it skips the rest of them. (Within a single schema, where
    names are unique, paging is verified correct and still used.)

    So the prime asks for everything at once and reports `usable=False` if
    the response came back at or over the chunk threshold. That check reads
    as 'this is too big to take in one bite' rather than 'Snowflake
    truncated us': a SHOW result at the threshold may or may not be
    complete -- SHOW COLUMNS has been seen returning 11511 rows against a
    10000 limit -- and either way the answer is the same, fall back to
    fetching schema by schema, where each response is small enough to trust
    whole.
    """
    assert scope.object_type is not None
    assert scope.database is not None
    cap = config.get(PRIME_CHUNK_THRESHOLD)
    rows = gateway.show_objects_in_database(
        scope.object_type,
        scope.database,
        after=None,
        limit=cap,
        allow_reconnect=allow_reconnect,
    )
    return rows, len(rows) < cap
