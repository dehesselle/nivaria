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

import itertools
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from snowcone.gateway.gateway import SnowflakeGateway
from snowcone.gateway.queries import MAX_PAGE_SIZE
from snowcone.models import Scope, ScopeKind
from snowcone.settings import PIPE_HISTORY_FETCH_DAYS, TASK_HISTORY_FETCH_DAYS, config

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

    def next_page(self, cursor: str, generation: int) -> FetchJob:
        return FetchJob(
            priority=self.priority,
            seq=next(_sequence),
            scope=self.scope,
            cursor=cursor,
            generation=generation,
        )


#: The column a SHOW result is keyed and paged by.
NAME_COLUMN = "name"


def fetch_page(
    gateway: SnowflakeGateway,
    scope: Scope,
    cursor: str | None,
    limit: int,
    *,
    allow_reconnect: bool = True,
    pipe_target: tuple[str, str, str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch one page, returning (rows, next_cursor).

    `next_cursor` is the last row's name when the page came back full, and
    None when it did not -- a short page means the end of the result, which is
    what terminates the continuation chain.

    `allow_reconnect` is passed straight through to the gateway: false for a
    background-priority job, so a dead pooled connection is never repaired by
    opening a new, possibly-interactive session while nobody is watching --
    see `ConnectionPool.acquire`.

    `pipe_target` is only meaningful for ScopeKind.PIPE_HISTORY, whose query
    needs the pipe's target table (catalog, schema, table) -- a piece of
    context this function has no way to resolve itself (it only has a
    gateway, not the repository, so it cannot look up cached data). The
    caller (`FetchService._execute`) resolves it before calling here; every
    other scope kind ignores this parameter.
    """
    match scope.kind:
        case ScopeKind.DATABASES:
            rows = gateway.show_databases(
                after=cursor, limit=limit, allow_reconnect=allow_reconnect
            )
        case ScopeKind.SCHEMAS:
            assert scope.database is not None
            rows = gateway.show_schemas(
                scope.database,
                after=cursor,
                limit=limit,
                allow_reconnect=allow_reconnect,
            )
        case ScopeKind.OBJECTS:
            assert scope.object_kind is not None
            assert scope.database is not None
            assert scope.schema is not None
            rows = gateway.show_objects(
                scope.object_kind,
                scope.database,
                scope.schema,
                after=cursor,
                limit=limit,
                allow_reconnect=allow_reconnect,
            )
        case ScopeKind.COLUMNS:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None
            # A single table's columns are a small, bounded result, so this
            # one is deliberately not paged.
            rows = gateway.show_columns(
                scope.database,
                scope.schema,
                scope.table,
                allow_reconnect=allow_reconnect,
            )
            return rows, None
        case ScopeKind.TABLE_STATS:
            assert scope.database is not None
            assert scope.schema is not None
            # A schema's table count is bounded, same reasoning as
            # show_columns being deliberately unpaged.
            return (
                gateway.table_stats(
                    scope.database, scope.schema, allow_reconnect=allow_reconnect
                ),
                None,
            )
        case ScopeKind.VIEW_STATS:
            assert scope.database is not None
            assert scope.schema is not None
            # A schema's view count is bounded, same reasoning as
            # TABLE_STATS being deliberately unpaged.
            return (
                gateway.view_stats(
                    scope.database, scope.schema, allow_reconnect=allow_reconnect
                ),
                None,
            )
        case ScopeKind.MATERIALIZED_VIEW_STATS:
            assert scope.database is not None
            assert scope.schema is not None
            # A schema's materialized-view count is bounded, same reasoning
            # as TABLE_STATS/VIEW_STATS being deliberately unpaged.
            return (
                gateway.materialized_view_stats(
                    scope.database, scope.schema, allow_reconnect=allow_reconnect
                ),
                None,
            )
        case ScopeKind.PIPE_ACTIVITY:
            assert scope.database is not None
            assert scope.schema is not None
            # Bounded by pipe count, not by history depth -- see
            # queries.pipe_activity's QUALIFY.
            fetch_days = config.get(PIPE_HISTORY_FETCH_DAYS)
            return (
                gateway.pipe_activity(
                    scope.database,
                    scope.schema,
                    fetch_days,
                    allow_reconnect=allow_reconnect,
                ),
                None,
            )
        case ScopeKind.PIPE_ACTIVITY_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None  # the pipe's name
            # Bounded to one pipe's history -- no target-table resolution
            # needed, unlike PIPE_HISTORY, since ACCOUNT_USAGE.COPY_HISTORY
            # has an ordinary PIPE_NAME column to filter on directly.
            fetch_days = config.get(PIPE_HISTORY_FETCH_DAYS)
            return (
                gateway.pipe_activity_history(
                    scope.database,
                    scope.schema,
                    scope.table,
                    fetch_days,
                    allow_reconnect=allow_reconnect,
                ),
                None,
            )
        case ScopeKind.PIPE_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None  # the pipe's name
            if pipe_target is None:
                raise ValueError(
                    f"cannot determine the target table for pipe {scope.table} "
                    "(no cached pipe-activity data to resolve it from yet)"
                )
            target_catalog, target_schema, target_table = pipe_target
            return (
                gateway.pipe_history(
                    target_catalog,
                    target_schema,
                    target_table,
                    scope.table,
                    allow_reconnect=allow_reconnect,
                ),
                None,
            )
        case ScopeKind.TASK_ACTIVITY:
            assert scope.database is not None
            assert scope.schema is not None
            # Bounded by task count, not by history depth -- see
            # queries.task_activity's QUALIFY.
            fetch_days = config.get(TASK_HISTORY_FETCH_DAYS)
            return (
                gateway.task_activity(
                    scope.database,
                    scope.schema,
                    fetch_days,
                    allow_reconnect=allow_reconnect,
                ),
                None,
            )
        case ScopeKind.TASK_ACTIVITY_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None  # the task's name
            # Bounded to one task's history -- no target-table resolution
            # needed, unlike PIPE_HISTORY: ACCOUNT_USAGE.TASK_HISTORY has an
            # ordinary NAME column to filter on directly.
            fetch_days = config.get(TASK_HISTORY_FETCH_DAYS)
            return (
                gateway.task_activity_history(
                    scope.database,
                    scope.schema,
                    scope.table,
                    fetch_days,
                    allow_reconnect=allow_reconnect,
                ),
                None,
            )
        case ScopeKind.TASK_HISTORY:
            assert scope.database is not None
            assert scope.schema is not None
            assert scope.table is not None  # the task's name
            # Unlike PIPE_HISTORY, TASK_NAME/DATABASE_NAME/SCHEMA_NAME are
            # direct named arguments a task's own Scope already carries, so
            # no resolved target parameter is needed here at all.
            return (
                gateway.task_history(
                    scope.database,
                    scope.schema,
                    scope.table,
                    allow_reconnect=allow_reconnect,
                ),
                None,
            )
        case ScopeKind.GRANTS:
            assert scope.database is not None
            # A schema or database's grant list is bounded, same reasoning
            # as show_columns/table_stats being deliberately unpaged.
            if scope.schema is not None:
                return (
                    gateway.show_grants_on_schema(
                        scope.database, scope.schema, allow_reconnect=allow_reconnect
                    ),
                    None,
                )
            return (
                gateway.show_grants_on_database(
                    scope.database, allow_reconnect=allow_reconnect
                ),
                None,
            )
        case ScopeKind.TAG_REFERENCES:
            assert scope.database is not None
            if scope.schema is not None:
                return (
                    gateway.tag_references_schema(
                        scope.database, scope.schema, allow_reconnect=allow_reconnect
                    ),
                    None,
                )
            return (
                gateway.tag_references_database(
                    scope.database, allow_reconnect=allow_reconnect
                ),
                None,
            )
        case ScopeKind.WAREHOUSES:
            # Small, flat, unpaged lists -- see queries.show_warehouses.
            return gateway.show_warehouses(allow_reconnect=allow_reconnect), None
        case ScopeKind.ROLES:
            return gateway.show_roles(allow_reconnect=allow_reconnect), None
        case ScopeKind.DATABASE_PRIME:
            raise ValueError(
                "a prime scope is fetched by fetch_database, not fetch_page"
            )
        case ScopeKind.COLUMNS_PRIME:
            raise ValueError(
                "a columns-prime scope is fetched by fetch_columns_database/"
                "fetch_columns_schema, not fetch_page"
            )

    if len(rows) < limit or not rows:
        return rows, None
    return rows, str(rows[-1][NAME_COLUMN])


#: Pushed once per worker to end its loop. seq=-1 with the lowest priority
#: value sorts it ahead of every real job, so stopping is prompt rather than
#: queued behind whatever work is outstanding.
SHUTDOWN = FetchJob(priority=Priority.INTERACTIVE, seq=-1, shutdown=True)


def fetch_database(
    gateway: SnowflakeGateway,
    scope: Scope,
    cap: int = MAX_PAGE_SIZE,
    *,
    allow_reconnect: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Every object of one kind in a database. Returns (rows, usable).

    Deliberately a single unpaged statement, unlike every other fetch here.

    Keyset paging is unsafe across a whole database: `LIMIT n FROM 'name'`
    resumes after an object *name*, and names are not unique across schemas --
    an ORDERS table in forty schemas is forty rows sharing one cursor value.
    Resuming after it skips the rest of them. (Within a single schema, where
    names are unique, paging is verified correct and still used.)

    So the prime asks for everything at once and reports `usable=False` if the
    result came back at Snowflake's 10k SHOW cap, since at that point the
    result is truncated and there is no safe way to ask for the remainder.
    The caller falls back to fetching schema by schema.
    """
    assert scope.object_kind is not None
    assert scope.database is not None
    rows = gateway.show_objects_in_database(
        scope.object_kind,
        scope.database,
        after=None,
        limit=cap,
        allow_reconnect=allow_reconnect,
    )
    return rows, len(rows) < cap


def fetch_columns_database(
    gateway: SnowflakeGateway,
    scope: Scope,
    cap: int = MAX_PAGE_SIZE,
    *,
    allow_reconnect: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Every column of every table/view in a database. Returns (rows, usable).

    Same shape and same truncation caveat as `fetch_database`: a single
    unpaged statement, `usable=False` when it came back at Snowflake's 10k
    SHOW cap, and the caller falls back to fetching schema by schema.
    """
    assert scope.database is not None
    rows = gateway.show_columns_in_database(
        scope.database, after=None, limit=cap, allow_reconnect=allow_reconnect
    )
    return rows, len(rows) < cap


def fetch_columns_schema(
    gateway: SnowflakeGateway,
    scope: Scope,
    cap: int = MAX_PAGE_SIZE,
    *,
    allow_reconnect: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Every column of every table/view in one schema. Returns (rows, usable).

    The fallback chunk when `fetch_columns_database` was truncated -- see
    there. `usable=False` here falls back further, to one job per table.
    """
    assert scope.database is not None
    assert scope.schema is not None
    rows = gateway.show_columns_in_schema(
        scope.database,
        scope.schema,
        after=None,
        limit=cap,
        allow_reconnect=allow_reconnect,
    )
    return rows, len(rows) < cap
