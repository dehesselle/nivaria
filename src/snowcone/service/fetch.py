"""The background fetch scheduler.

Nothing here touches a widget. A worker writes its page into sqlite and then
calls `notify(scope, complete)`; the UI layer is responsible for marshalling
that onto the event loop (`App.call_from_thread`) and routing it to whichever
single node or view cares about that scope.

Scopes are deliberately **independent of one another**. It is tempting to think
a `tables:MYDB.PUBLIC` fetch must wait for `schemas:MYDB` to finish refreshing,
but `SHOW TABLES IN SCHEMA MYDB.PUBLIC` is a self-contained query either way,
so the two just run concurrently on different pool connections. If the schema
really was dropped, the tables job fails, that failure lands in the ledger, and
the schema refresh removes the node on its own sweep. No dependency graph, no
ordering constraints, and no deadlock surface.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from snowcone.cache.repository import Repository
from snowcone.gateway.gateway import SnowflakeGateway
from snowcone.models import (
    ObjectType,
    Scope,
    ScopeType,
    columns_prime_scope,
    columns_scope,
    objects_scope,
    schemas_scope,
)
from snowcone.service.jobs import (
    SHUTDOWN,
    FetchJob,
    Priority,
    fetch_columns_database,
    fetch_columns_schema,
    fetch_database,
    fetch_page,
)

_log = logging.getLogger(__name__)

Notify = Callable[[Scope, bool], None]
OnError = Callable[[Scope, str], None]


class FetchService:
    def __init__(
        self,
        gateway: SnowflakeGateway,
        repo: Repository,
        *,
        workers: int = 1,
        page_size: int = 1000,
        notify: Notify | None = None,
        on_error: OnError | None = None,
    ) -> None:
        self._gateway = gateway
        self._repo = repo
        self._workers = max(1, workers)
        self._page_size = page_size
        self._notify = notify
        self._on_error = on_error

        self._queue: queue.PriorityQueue[FetchJob] = queue.PriorityQueue()
        self._lock = threading.Lock()
        #: The one authoritative job per scope. A job dequeued that is no
        #: longer the value here has been superseded and is skipped -- which is
        #: how a priority upgrade works, since PriorityQueue has no decrease-key.
        self._pending: dict[Scope, FetchJob] = {}
        self._running: set[Scope] = set()
        #: Scopes cancelled while a page was already in flight. Checked before
        #: a continuation is enqueued, since the running page cannot be stopped.
        self._cancelled: set[Scope] = set()
        self._threads: list[threading.Thread] = []
        self._started = False

    # -------------------------------------------------------------- control

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self._workers):
            thread = threading.Thread(
                target=self._run, name=f"snowcone-fetch-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 2.0) -> None:
        if not self._started:
            return
        for _ in self._threads:
            self._queue.put(SHUTDOWN)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        self._started = False

    # --------------------------------------------------------------- public

    def request(self, scope: Scope, priority: Priority = Priority.INTERACTIVE) -> bool:
        """Ask for a scope to be refreshed. Returns whether a job was queued.

        Deduplicated: a scope already running is left alone, and a scope
        already queued is only re-queued when this request outranks it. That
        is what stops a burst of expand events from queueing the same work
        five times over.
        """
        with self._lock:
            self._cancelled.discard(scope)
            if scope in self._running:
                return False
            existing = self._pending.get(scope)
            if existing is not None and existing.priority <= priority:
                return False
            job = FetchJob(priority=priority, scope=scope)
            self._pending[scope] = job
        self._queue.put(job)
        return True

    def cancel_where(self, predicate: Callable[[Scope], bool]) -> list[Scope]:
        """Drop queued work matching `predicate`, e.g. on a database switch.

        A page already in flight still finishes -- it cannot be interrupted --
        but its continuation is suppressed, so the chain stops after at most
        one more page.
        """
        with self._lock:
            doomed = [scope for scope in self._pending if predicate(scope)]
            for scope in doomed:
                del self._pending[scope]
            for scope in self._running:
                if predicate(scope):
                    self._cancelled.add(scope)
                    doomed.append(scope)
        return doomed

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def is_busy(self, scope: Scope) -> bool:
        """Whether this scope is queued or mid-fetch, for a spinner."""
        with self._lock:
            return scope in self._running or scope in self._pending

    # -------------------------------------------------------------- workers

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job.shutdown:
                return
            if not self._claim(job):
                continue
            try:
                self._execute(job)
            finally:
                with self._lock:
                    self._running.discard(job.scope)

    def _claim(self, job: FetchJob) -> bool:
        """Take ownership of a dequeued job, unless it has been superseded."""
        with self._lock:
            if self._pending.get(job.scope) is not job:
                return False
            del self._pending[job.scope]
            self._running.add(job.scope)
            return True

    def _execute(self, job: FetchJob) -> None:
        if job.scope.type is ScopeType.DATABASE_PRIME:
            self._execute_prime(job)
            return
        if job.scope.type is ScopeType.COLUMNS_PRIME:
            self._execute_columns_prime(job)
            return
        scope = job.scope
        generation = job.generation
        # Never let an unattended sweep open a new authenticated session --
        # see ConnectionPool.acquire.
        allow_reconnect = job.priority is not Priority.BACKGROUND
        try:
            if generation == 0:
                generation = self._repo.begin_pass(scope)
            pipe_target = None
            if scope.type is ScopeType.PIPE_HISTORY:
                assert scope.database is not None
                assert scope.schema is not None
                assert scope.table is not None
                pipe_target = self._repo.pipe_target_table(
                    scope.database, scope.schema, scope.table
                )
            rows, cursor = fetch_page(
                self._gateway,
                scope,
                job.cursor,
                self._page_size,
                allow_reconnect=allow_reconnect,
                pipe_target=pipe_target,
            )
        # A worker thread boundary: the driver, the network and sqlite all
        # raise unrelated exception types, and any escape here would kill the
        # thread and silently strand every later job.
        except Exception as error:  # noqa: BLE001
            message = str(error) or error.__class__.__name__
            if generation:
                self._repo.fail_pass(scope, generation, message)
            self._report(scope, message)
            self._announce(scope, complete=True)
            return

        if not self._repo.write_page(scope, generation, rows, cursor):
            # A newer pass claimed this scope while the page was in flight.
            return

        if cursor is None:
            self._repo.finish_pass(scope, generation)
            self._announce(scope, complete=True)
            return

        self._announce(scope, complete=False)
        with self._lock:
            if scope in self._cancelled:
                self._cancelled.discard(scope)
                return
            continuation = job.next_page(cursor, generation)
            self._pending[scope] = continuation
        self._queue.put(continuation)

    def _execute_prime(self, job: FetchJob) -> None:
        """Fill every schema's scope in a database from one statement.

        Measured against a live account, this is roughly an order of magnitude
        cheaper than visiting each schema: ~1s for 2145 tables across 54
        schemas, versus 54 round trips at ~187ms each.
        """
        scope = job.scope
        assert scope.object_type is not None
        assert scope.database is not None
        object_type: ObjectType = scope.object_type
        database: str = scope.database
        allow_reconnect = job.priority is not Priority.BACKGROUND
        try:
            rows, usable = fetch_database(
                self._gateway, scope, allow_reconnect=allow_reconnect
            )
        # See _execute: a worker thread boundary.
        except Exception as error:  # noqa: BLE001
            message = str(error) or error.__class__.__name__
            self._report(scope, message)
            self._announce(scope, complete=True)
            return

        if not usable:
            # Truncated at Snowflake's SHOW cap and not safely pageable, so
            # fall back to a job per schema. Slower, but correct.
            self._fan_out(object_type, database, job.priority)
            self._announce(scope, complete=True)
            return

        grouped: dict[Scope, list[dict[str, Any]]] = {}
        # Every schema already known gets an entry, so one that returned no
        # objects still completes a pass and reads as "fetched, none" rather
        # than "never fetched" -- otherwise its node would never settle.
        for schema in self._repo.schemas(schemas_scope(database)).rows:
            grouped[objects_scope(object_type, database, schema.name)] = []
        for row in rows:
            schema_name = row.get("schema_name")
            if not schema_name:
                continue
            grouped.setdefault(
                objects_scope(object_type, database, str(schema_name)), []
            ).append(row)

        self._repo.replace_grouped(grouped)
        for filled in grouped:
            self._announce(filled, complete=True)
        self._announce(scope, complete=True)

    def _fan_out(
        self, object_type: ObjectType, database: str, priority: Priority
    ) -> None:
        """Queue one job per known schema, the correct-but-slower path."""
        for schema in self._repo.schemas(schemas_scope(database)).rows:
            self.request(objects_scope(object_type, database, schema.name), priority)

    def _execute_columns_prime(self, job: FetchJob) -> None:
        """Fill many tables' COLUMNS scopes from one statement.

        `schema is None` tries the whole database in one call; `schema` set is
        the fallback chunk for one schema after the whole-database call came
        back truncated. Either way, a usable result is grouped by table and
        written with `replace_grouped` -- unlike `_execute_prime`, there is no
        pre-seeding of empty scopes needed, since a real table always has at
        least one column.
        """
        scope = job.scope
        assert scope.database is not None
        database = scope.database
        allow_reconnect = job.priority is not Priority.BACKGROUND
        try:
            if scope.schema is None:
                rows, usable = fetch_columns_database(
                    self._gateway, scope, allow_reconnect=allow_reconnect
                )
            else:
                rows, usable = fetch_columns_schema(
                    self._gateway, scope, allow_reconnect=allow_reconnect
                )
        # See _execute: a worker thread boundary.
        except Exception as error:  # noqa: BLE001
            message = str(error) or error.__class__.__name__
            self._report(scope, message)
            self._announce(scope, complete=True)
            return

        if not usable:
            if scope.schema is None:
                self._fan_out_columns_by_schema(
                    database, job.priority, allow_reconnect=allow_reconnect
                )
            else:
                self._fan_out_columns_by_table(database, scope.schema, job.priority)
            self._announce(scope, complete=True)
            return

        grouped: dict[Scope, list[dict[str, Any]]] = {}
        for row in rows:
            table_schema = row.get("TABLE_SCHEMA")
            table_name = row.get("TABLE_NAME")
            if not table_schema or not table_name:
                continue
            grouped.setdefault(
                columns_scope(database, str(table_schema), str(table_name)), []
            ).append(row)

        self._repo.replace_grouped(grouped)
        for filled in grouped:
            self._announce(filled, complete=True)
        self._announce(scope, complete=True)

    def _fan_out_columns_by_schema(
        self, database: str, priority: Priority, *, allow_reconnect: bool
    ) -> None:
        """Queue one columns-prime job per schema, the fallback chunk size.

        Pre-warming may be the first request ever made against a database
        nobody has browsed yet, so the SCHEMAS scope this would normally read
        can still be empty -- there is no dependency graph to lean on here
        (see DESIGN.md), so a cold cache is repaired by asking Snowflake for
        the schema names directly rather than waiting on another scope to
        finish first. The SCHEMAS scope is also requested so the tree
        benefits from this too, not just the fallback fan-out.
        """
        cached = self._repo.schemas(schemas_scope(database)).rows
        if cached:
            names = [schema.name for schema in cached]
        else:
            try:
                rows = self._gateway.show_schemas(
                    database, allow_reconnect=allow_reconnect
                )
            except Exception:  # noqa: BLE001 -- see _execute
                rows = []
            names = [str(row["name"]) for row in rows if row.get("name")]
            self.request(schemas_scope(database), priority)
        for name in names:
            self.request(columns_prime_scope(database, name), priority)

    def _fan_out_columns_by_table(
        self, database: str, schema: str, priority: Priority
    ) -> None:
        """Queue one ordinary per-table COLUMNS job, the last-resort chunk.

        Only reached when a single schema's columns still overflow the SHOW
        cap by itself. Relies on that schema's TABLE/VIEW object lists
        already being cached -- an accepted gap for this rare, deep fallback
        rather than another live round trip: the menu action that starts a
        columns prime always also starts a table/view prime for the whole
        database alongside it.
        """
        for object_type in (ObjectType.TABLE, ObjectType.VIEW):
            for obj in self._repo.objects(
                objects_scope(object_type, database, schema)
            ).rows:
                self.request(columns_scope(database, schema, obj.name), priority)

    def _announce(self, scope: Scope, *, complete: bool) -> None:
        if self._notify is None:
            return
        # A listener raising here would kill the worker thread and silently
        # strand every later job, so it is contained.
        try:
            self._notify(scope, complete)
        except Exception:  # noqa: BLE001 -- see _execute
            _log.exception("cache-update listener failed for %s", scope)

    def _report(self, scope: Scope, message: str) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(scope, message)
        except Exception:  # noqa: BLE001 -- see _execute
            _log.exception("error listener failed for %s", scope)
