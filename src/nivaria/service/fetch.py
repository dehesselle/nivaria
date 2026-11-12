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

from nivaria import journal
from nivaria.cache.repository import Prune, Repository, plan_window
from nivaria.journal import Reason
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
from nivaria.settings import MAX_PAGES_PER_PASS, PAGE_SIZE, config
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
        notify: Notify | None = None,
        on_error: OnError | None = None,
    ) -> None:
        self._gateway = gateway
        self._repo = repo
        self._workers = max(1, workers)
        self._notify = notify
        self._on_error = on_error

        self._queue: queue.PriorityQueue[FetchJob] = queue.PriorityQueue()
        self._lock = threading.Lock()
        #: The one authoritative job per scope. A job dequeued that is no
        #: longer the value here has been superseded and is skipped -- which is
        #: how a priority upgrade works, since PriorityQueue has no decrease-key.
        self._pending: dict[Scope, FetchJob] = {}
        #: The job that owns each scope with a statement in flight. By job, not
        #: just by scope, so a worker done with a page cannot clear the claim
        #: of the worker that has already picked up the next one.
        self._running: dict[Scope, FetchJob] = {}
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

    def request(
        self,
        scope: Scope,
        priority: Priority = Priority.INTERACTIVE,
        *,
        reason: Reason = Reason.UNKNOWN,
        detail: str = "",
    ) -> bool:
        """Ask for a scope to be refreshed. Returns whether a job was queued.

        Deduplicated: a scope already running is left alone, and a scope
        already queued is only re-queued when this request outranks it. That
        is what stops a burst of expand events from queueing the same work
        five times over.

        `reason` and `detail` are what the journal says about it: the trigger,
        and why the scope was due.
        """
        superseded: Reason | None = None
        skip: str | None = None
        with self._lock:
            self._cancelled.discard(scope)
            running = self._running.get(scope)
            existing = self._pending.get(scope)
            if running is not None:
                skip = f"already running ({running.reason})"
            elif existing is not None and existing.priority <= priority:
                skip = f"already queued ({existing.reason})"
            else:
                superseded = existing.reason if existing is not None else None
                job = FetchJob(priority=priority, scope=scope, reason=reason)
                self._pending[scope] = job
        if skip is not None:
            journal.skipped(scope, reason, skip)
            return False
        journal.queued(
            scope,
            reason,
            detail,
            priority=priority.name.lower(),
            supersedes=superseded,
        )
        self._queue.put(job)
        return True

    def cancel_where(
        self, predicate: Callable[[Scope], bool], why: str = "cancelled"
    ) -> list[Scope]:
        """Drop queued work matching `predicate`, e.g. on a database switch.

        A page already in flight still finishes -- it cannot be interrupted --
        but its continuation is suppressed, so the chain stops after at most
        one more page. `why` is what the journal says about it.
        """
        with self._lock:
            queued = [scope for scope in self._pending if predicate(scope)]
            for scope in queued:
                del self._pending[scope]
            running = [scope for scope in self._running if predicate(scope)]
            self._cancelled.update(running)
        for scope in queued:
            journal.dropped(scope, f"{why}: taken off the queue")
        for scope in running:
            journal.dropped(scope, f"{why}: still running, no further page")
        return queued + running

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
                self._release(job)

    def _release(self, job: FetchJob) -> None:
        """Stop counting `job` as running, if nothing has taken over its scope.

        Called as soon as its result is committed, before anyone is told.
        Telling the UI waits for its event loop, however busy that is, and
        whatever it does on hearing -- retrying a failed scope after a warehouse
        switch, say -- must not find the finished job still counted as busy.
        """
        with self._lock:
            if self._running.get(job.scope) is job:
                del self._running[job.scope]

    def _claim(self, job: FetchJob) -> bool:
        """Take ownership of a dequeued job, unless it has been superseded."""
        with self._lock:
            # A worker can dequeue real work just before stop() queues its
            # SHUTDOWN; that statement would belong to nobody.
            if self._stopped or self._pending.get(job.scope) is not job:
                return False
            del self._pending[job.scope]
            self._running[job.scope] = job
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
                    )
                ]
            journal.sent(
                scope,
                job.reason,
                page=job.page,
                window=window,
                reconnect=allow_reconnect,
            )
            sent_at = time.monotonic()
            page = fetch_page(
                self._gateway,
                scope,
                job.cursor,
                config.get(PAGE_SIZE),
                allow_reconnect=allow_reconnect,
                pipe_target=pipe_target,
                pipe_names=pipe_names,
                window_seconds=window.seconds if window is not None else None,
            )
            journal.done(
                scope,
                len(page.rows),
                time.monotonic() - sent_at,
                _page_note(page),
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
        scope = job.scope
        if self._stopped:
            journal.dropped(scope, "fetching was stopped, the result is discarded")
            return
        cursor = page.cursor
        if not self._repo.write_page(scope, generation, page.rows):
            # A newer pass claimed this scope while the page was in flight.
            journal.dropped(scope, "a newer pass took over, the page is discarded")
            return

        # Two guards on the continuation chain, and neither is belt-and-braces:
        # the chain re-issues the same statement with a new cursor and trusts
        # the result to shrink, which is precisely the assumption Snowflake
        # has been observed to break.
        if cursor is not None and cursor == job.cursor:
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
            cursor = None
        if cursor is None:
            self._repo.finish_pass(
                scope, generation, truncated=page.truncated, **finish
            )
            self._release(job)
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
            self._release(job)
            journal.failed(scope, message)
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
            journal.sent(job.scope, job.reason, reconnect=allow_reconnect)
            rows, usable = fetch_database(
                self._gateway, job.scope, allow_reconnect=allow_reconnect
            )
            journal.done(
                job.scope,
                len(rows),
                time.time() - started_at,
                "" if usable else "at the chunk threshold, fetching per schema",
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
        scope = job.scope
        if self._stopped:
            journal.dropped(scope, "fetching was stopped, the result is discarded")
            return
        assert scope.object_type is not None
        assert scope.database is not None
        object_type: ObjectType = scope.object_type
        database: str = scope.database

        if not usable:
            # Truncated at Snowflake's SHOW cap and not safely pageable, so
            # fall back to a job per schema. Slower, but correct.
            self._fan_out(object_type, database, job.priority, scope)
            self._release(job)
            self._announce(scope, complete=True)
            return

        grouped: dict[Scope, list[dict[str, Any]]] = {}
        # Every schema already known gets an entry, so one that returned no
        # objects still completes a pass and reads as "fetched, none" rather
        # than "never fetched" -- otherwise its node would never settle.
        for schema in self._repo.schemas(schemas_scope(database)):
            grouped[objects_scope(object_type, database, schema.name)] = []
        for row in rows:
            schema_name = row.get("schema_name")
            if not schema_name:
                continue
            grouped.setdefault(
                objects_scope(object_type, database, str(schema_name)), []
            ).append(row)

        self._repo.replace_grouped(grouped, started_at)
        self._release(job)
        for filled in grouped:
            self._announce(filled, complete=True)
        self._announce(scope, complete=True)

    def _fan_out(
        self, object_type: ObjectType, database: str, priority: Priority, prime: Scope
    ) -> None:
        """Queue one job per known schema, the correct-but-slower path."""
        for schema in self._repo.schemas(schemas_scope(database)):
            self.request(
                objects_scope(object_type, database, schema.name),
                priority,
                reason=Reason.PRIME_FAN_OUT,
                detail=f"for {prime}",
            )

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
        scope = job.scope
        message = str(error) or error.__class__.__name__
        if self._stopped:
            journal.dropped(scope, f"fetching was stopped, discarding: {message}")
            return
        journal.failed(scope, message)
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
                self.request(
                    container,
                    job.priority,
                    reason=Reason.MISSING_OBJECT,
                    detail=f"{scope} does not exist",
                )
        self._release(job)
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


def _page_note(page: Page) -> str:
    """What the journal adds to a page that came back."""
    if page.cursor is not None:
        return "more pages follow"
    if page.truncated:
        return "row cap reached"
    return ""
