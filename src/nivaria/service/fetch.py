# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The background fetch scheduler.

Nothing here touches a widget. A worker writes its page into sqlite and then
calls `notify(scope, complete)`; the UI layer is responsible for marshalling
that onto the event loop (`App.call_from_thread`) and routing it to whichever
single node or view cares about that scope.

Scopes are deliberately **independent of one another**. It is tempting to think
a `tables:MYDB.PUBLIC` fetch must wait for `schemas:MYDB` to finish refreshing,
but `SHOW TABLES IN SCHEMA MYDB.PUBLIC` is a self-contained query either way,
so the two just run concurrently on different pool connections. If the schema
really was dropped, the tables job fails with "does not exist", that failure
lands in the ledger, and the job asks for the listing that contains it
(`models.containing_scopes`) -- whose refresh removes the node. That request
is the only link between scopes, and it is made after the fact, never waited
on: no dependency graph, no ordering constraints, and no deadlock surface.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from nivaria.cache.repository import Prune, Repository, plan_window
from nivaria.models import (
    ObjectType,
    Scope,
    ScopeType,
    containing_scopes,
    objects_scope,
    schemas_scope,
)
from nivaria.service.jobs import (
    SHUTDOWN,
    FetchJob,
    Page,
    Priority,
    fetch_database,
    fetch_page,
    log_policy,
)
from nivaria.settings import MAX_PAGES_PER_PASS, config
from nivaria.snowflake.gateway import SnowflakeGateway, is_missing_object

_log = logging.getLogger(__name__)

Notify = Callable[[Scope, bool], None]
#: (scope, message, missing) -- `missing` is set when the failure was the
#: object itself being gone, which the UI reports more gently.
OnError = Callable[[Scope, str, bool], None]


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
        #: Set by stop(), never cleared. stop() gives up on a worker still
        #: mid-query, and by the time that query returns the caller has moved
        #: on: a reconnect has repointed the UI at another identity, and a
        #: role switch has run USE ROLE on the shared pool, so the gateway's
        #: retry would even run under the new role and land in the old role's
        #: cache. Whatever such a worker brings back belongs to nobody, so it
        #: is dropped -- no write, no notify, no report -- leaving the cache as
        #: a quit mid-fetch already does.
        self._stopped = False

    # -------------------------------------------------------------- control

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self._workers):
            thread = threading.Thread(
                target=self._run, name=f"nivaria-fetch-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 2.0) -> None:
        """Ask every worker to exit and wait up to `timeout` for each.

        `timeout=0` only signals: quitting waits for `running_count` to drain
        on the event loop instead, so the wait stays visible and can be cut
        short.
        """
        # Under the lock so _claim cannot start a statement after this:
        # running_count may only fall from here on.
        with self._lock:
            self._stopped = True
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

    @property
    def running_count(self) -> int:
        """Statements in flight, which a stopped service still waits out."""
        with self._lock:
            return len(self._running)

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
            # A worker can dequeue real work just before stop() queues its
            # SHUTDOWN; that statement would belong to nobody.
            if self._stopped or self._pending.get(job.scope) is not job:
                return False
            del self._pending[job.scope]
            self._running.add(job.scope)
            return True

    def _execute(self, job: FetchJob) -> None:
        if job.scope.type is ScopeType.DATABASE_PRIME:
            self._execute_prime(job)
            return
        scope = job.scope
        generation = job.generation
        # Never let an unattended sweep open a new authenticated session --
        # see ConnectionPool.acquire.
        allow_reconnect = job.priority is not Priority.BACKGROUND
        # A log scope asks only for what is new since its last pass, unless
        # its ledger says a full pass is due -- see repository.plan_window.
        # Planned from the entry as it stands before this pass claims it.
        policy = log_policy(scope)
        window = None
        finish: dict[str, Any] = {}
        try:
            if policy is not None:
                started = time.time()
                window = plan_window(
                    self._repo.entry(scope),
                    policy.fetch_seconds,
                    policy.margin,
                    started,
                )
                if window.full:
                    finish["covered_from"] = started - policy.fetch_seconds
                else:
                    finish["prune"] = Prune(
                        cutoff=started - policy.fetch_seconds,
                        max_rows=policy.max_rows,
                    )
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
            pipe_names = None
            if scope.type is ScopeType.PIPE_STATUS:
                assert scope.database is not None
                assert scope.schema is not None
                # Read again for every page, so a pass that outlives a pipe
                # list refresh carries on with the newer list.
                pipe_names = [
                    obj.name
                    for obj in self._repo.objects(
                        objects_scope(ObjectType.PIPE, scope.database, scope.schema)
                    ).rows
                ]
            page = fetch_page(
                self._gateway,
                scope,
                job.cursor,
                self._page_size,
                allow_reconnect=allow_reconnect,
                pipe_target=pipe_target,
                pipe_names=pipe_names,
                window_seconds=window.seconds if window is not None else None,
            )
            self._store(job, page, generation, finish)
        # A worker thread boundary: the driver, the network and sqlite all
        # raise unrelated exception types, and any escape here would kill the
        # thread and silently strand every later job.
        except Exception as error:  # noqa: BLE001
            self._fail(job, error, generation)

    def _store(
        self, job: FetchJob, page: Page, generation: int, finish: dict[str, Any]
    ) -> None:
        """Write a fetched page to the cache and queue whatever comes next."""
        if self._stopped:
            return
        scope = job.scope
        cursor = page.cursor
        if not self._repo.write_page(scope, generation, page.rows, cursor):
            # A newer pass claimed this scope while the page was in flight.
            return

        if cursor is None:
            self._repo.finish_pass(
                scope, generation, truncated=page.truncated, **finish
            )
            self._announce(scope, complete=True)
            return

        # Two guards on the continuation chain, and neither is belt-and-braces:
        # the chain re-issues the same statement with a new cursor and trusts
        # the result to shrink, which is precisely the assumption Snowflake
        # has been observed to break.
        if cursor == job.cursor:
            # The cursor did not move, so this page is the last one's result
            # again -- a statement that accepted LIMIT/FROM and ignored both.
            # Not data loss: that response was the entire result, so the pass
            # completes normally. Worth a warning, because the statement
            # builder is supposed to have known this type is unpageable.
            _log.warning(
                "paging made no progress for %s (cursor stuck at %r); "
                "treating the page as the whole result",
                scope,
                cursor,
            )
            self._repo.finish_pass(scope, generation, truncated=page.truncated)
            self._announce(scope, complete=True)
            return
        if job.page + 1 >= config.get(MAX_PAGES_PER_PASS):
            # The cursor keeps moving but the result will not end. Fail
            # rather than truncate silently: fail_pass does not sweep, so
            # what was already on screen stays there and the error is
            # visible instead of a half-fetched scope looking complete.
            message = (
                f"stopped after {job.page + 1} pages -- paging is not "
                "reaching the end of this result"
            )
            self._repo.fail_pass(scope, generation, message)
            self._report(scope, message)
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
        allow_reconnect = job.priority is not Priority.BACKGROUND
        started_at = time.time()
        try:
            rows, usable = fetch_database(
                self._gateway, job.scope, allow_reconnect=allow_reconnect
            )
            self._store_prime(job, rows, usable, started_at)
        # See _execute: a worker thread boundary.
        except Exception as error:  # noqa: BLE001
            self._fail(job, error)

    def _store_prime(
        self,
        job: FetchJob,
        rows: list[dict[str, Any]],
        usable: bool,
        started_at: float,
    ) -> None:
        """Write a prime's rows to every schema's scope, or fan out instead."""
        if self._stopped:
            return
        scope = job.scope
        assert scope.object_type is not None
        assert scope.database is not None
        object_type: ObjectType = scope.object_type
        database: str = scope.database

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

        self._repo.replace_grouped(grouped, started_at)
        for filled in grouped:
            self._announce(filled, complete=True)
        self._announce(scope, complete=True)

    def _fan_out(
        self, object_type: ObjectType, database: str, priority: Priority
    ) -> None:
        """Queue one job per known schema, the correct-but-slower path."""
        for schema in self._repo.schemas(schemas_scope(database)).rows:
            self.request(objects_scope(object_type, database, schema.name), priority)

    def _fail(self, job: FetchJob, error: Exception, generation: int = 0) -> None:
        """Record and report a failed job, and react if its object is gone.

        A "does not exist" means the cache still lists something that was
        dropped elsewhere, so the listing it came from is asked for straight
        away rather than left for the TTL sweep -- otherwise the node stays
        on screen and every visit to it fails again. Requested as-is, not
        upgraded to a database-wide prime the way MainScreen.on_scope_needed
        upgrades OBJECTS: the per-schema SHOW is what fails in turn when the
        whole schema was dropped, carrying the refresh up to the schema list,
        while a prime would succeed and leave the schema node standing.
        """
        if self._stopped:
            return
        scope = job.scope
        message = str(error) or error.__class__.__name__
        missing = is_missing_object(error)
        if generation:
            # What made the pass fail, a full disk say, can stop this write
            # too. The user still has to hear about it, and the worker to live.
            try:
                self._repo.fail_pass(scope, generation, message)
            except Exception:  # noqa: BLE001 -- see _execute
                _log.exception("could not record the failure of %s", scope)
        if missing:
            for container in containing_scopes(scope):
                self.request(container, job.priority)
        self._report(scope, message, missing=missing)
        self._announce(scope, complete=True)

    def _announce(self, scope: Scope, *, complete: bool) -> None:
        if self._notify is None or self._stopped:
            return
        # A listener raising here would kill the worker thread and silently
        # strand every later job, so it is contained.
        try:
            self._notify(scope, complete)
        except Exception:  # noqa: BLE001 -- see _execute
            _log.exception("cache-update listener failed for %s", scope)

    def _report(self, scope: Scope, message: str, *, missing: bool = False) -> None:
        if self._on_error is None or self._stopped:
            return
        try:
            self._on_error(scope, message, missing)
        except Exception:  # noqa: BLE001 -- see _execute
            _log.exception("error listener failed for %s", scope)
