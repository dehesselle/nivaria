from __future__ import annotations

import threading
import time

import pytest

from snowcone.cache.db import Database
from snowcone.cache.repository import Repository
from snowcone.models import (
    CacheState,
    ObjectKind,
    databases_scope,
    objects_scope,
    schemas_scope,
)
from snowcone.service.fetch import FetchService
from snowcone.service.jobs import FetchJob, Priority, fetch_page
from tests.fakes import FakeGateway


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def make_service(gateway, repo, **kwargs) -> FetchService:
    service = FetchService(gateway, repo, **kwargs)
    service.start()
    return service


# ------------------------------------------------------- page-fetch dispatch


def test_a_short_page_ends_the_chain() -> None:
    """A page smaller than the limit means the result is exhausted; carrying
    on would re-query the same tail forever."""
    gateway = FakeGateway(databases=["ACME", "BETA"])
    rows, cursor = fetch_page(gateway, databases_scope(), None, limit=10)
    assert len(rows) == 2
    assert cursor is None


def test_a_full_page_yields_the_last_name_as_the_cursor() -> None:
    gateway = FakeGateway(databases=["ACME", "BETA", "GAMMA"])
    rows, cursor = fetch_page(gateway, databases_scope(), None, limit=2)
    assert [row["name"] for row in rows] == ["ACME", "BETA"]
    assert cursor == "BETA"


def test_an_empty_result_ends_the_chain() -> None:
    rows, cursor = fetch_page(FakeGateway(), databases_scope(), None, limit=10)
    assert rows == []
    assert cursor is None


def test_columns_are_fetched_in_one_shot() -> None:
    """A single table's columns are small and bounded, so paging them would
    only add round trips."""
    gateway = FakeGateway(columns={("MYDB", "PUBLIC", "ORDERS"): ["ID", "TOTAL"]})
    from snowcone.models import columns_scope

    rows, cursor = fetch_page(
        gateway, columns_scope("MYDB", "PUBLIC", "ORDERS"), None, limit=1
    )
    assert len(rows) == 2
    assert cursor is None


# -------------------------------------------------------------- end to end


def test_a_scope_is_fetched_and_lands_in_the_cache(repo) -> None:
    gateway = FakeGateway(databases=["ACME", "BETA"])
    service = make_service(gateway, repo)
    try:
        service.request(databases_scope())
        assert wait_until(
            lambda: repo.entry(databases_scope()).state is CacheState.COMPLETE
        )
    finally:
        service.stop()
    assert [d.name for d in repo.databases(databases_scope()).rows] == ["ACME", "BETA"]


def test_a_large_scope_arrives_in_pages(repo) -> None:
    """The cold-cache requirement: the tree must fill in visible steps rather
    than staying blank until the last page lands."""
    names = [f"DB_{index:03d}" for index in range(25)]
    gateway = FakeGateway(databases=names)
    seen: list[tuple[str, bool]] = []
    service = make_service(
        gateway,
        repo,
        page_size=10,
        notify=lambda scope, complete: seen.append((scope.key, complete)),
    )
    try:
        service.request(databases_scope())
        assert wait_until(lambda: seen and seen[-1][1] is True)
    finally:
        service.stop()

    # 10 + 10 + 5: three pages, so two partial announcements then a final one.
    assert [complete for _, complete in seen] == [False, False, True]
    assert len(repo.databases(databases_scope()).rows) == 25


def test_every_page_is_readable_as_soon_as_it_lands(repo) -> None:
    counts: list[int] = []
    gateway = FakeGateway(databases=[f"DB_{i:03d}" for i in range(25)])

    def on_page(scope, complete) -> None:
        counts.append(len(repo.databases(scope).rows))

    service = make_service(gateway, repo, page_size=10, notify=on_page)
    try:
        service.request(databases_scope())
        assert wait_until(lambda: len(counts) == 3)
    finally:
        service.stop()
    assert counts == [10, 20, 25]


def test_a_failing_fetch_records_the_error_and_keeps_stale_rows(repo) -> None:
    class Boom(FakeGateway):
        def show_databases(self, **kwargs):
            raise RuntimeError("network down")

    scope = databases_scope()
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, [{"name": "ACME"}], None)
    repo.finish_pass(scope, generation)

    errors: list[tuple[str, str]] = []
    service = make_service(
        Boom(), repo, on_error=lambda s, message: errors.append((s.key, message))
    )
    try:
        service.request(scope)
        assert wait_until(lambda: bool(errors))
    finally:
        service.stop()

    assert errors[0] == ("databases", "network down")
    assert repo.entry(scope).state is CacheState.ERROR
    assert [d.name for d in repo.databases(scope).rows] == ["ACME"]


def test_a_listener_that_raises_does_not_strand_the_worker(repo) -> None:
    """An escape from the notify callback would kill the thread and silently
    leave every later job unprocessed."""
    gateway = FakeGateway(databases=["ACME"], schemas={"MYDB": ["PUBLIC"]})

    def explode(scope, complete) -> None:
        raise RuntimeError("listener bug")

    service = make_service(gateway, repo, notify=explode)
    try:
        service.request(databases_scope())
        assert wait_until(
            lambda: repo.entry(databases_scope()).state is CacheState.COMPLETE
        )
        service.request(schemas_scope("MYDB"))
        assert wait_until(
            lambda: repo.entry(schemas_scope("MYDB")).state is CacheState.COMPLETE
        )
    finally:
        service.stop()


# ------------------------------------------------------------------- dedup


def test_requesting_a_queued_scope_again_is_dropped(repo) -> None:
    """A burst of expand events must not queue the same work five times."""
    service = FetchService(FakeGateway(), repo)
    scope = databases_scope()
    assert service.request(scope, Priority.INTERACTIVE) is True
    assert service.request(scope, Priority.INTERACTIVE) is False
    assert service.pending_count == 1


def test_a_lower_priority_request_does_not_displace_a_queued_one(repo) -> None:
    service = FetchService(FakeGateway(), repo)
    scope = databases_scope()
    service.request(scope, Priority.INTERACTIVE)
    assert service.request(scope, Priority.BACKGROUND) is False


def test_a_higher_priority_request_supersedes_a_queued_one(repo) -> None:
    """The user expanding a node has to overtake the TTL sweep that happened
    to queue the same scope a moment earlier."""
    service = FetchService(FakeGateway(), repo)
    scope = databases_scope()
    service.request(scope, Priority.BACKGROUND)
    assert service.request(scope, Priority.INTERACTIVE) is True
    assert service.pending_count == 1


def test_a_superseded_job_is_skipped_rather_than_run_twice(repo) -> None:
    gateway = FakeGateway(databases=["ACME"])
    service = FetchService(gateway, repo)
    scope = databases_scope()
    service.request(scope, Priority.BACKGROUND)
    service.request(scope, Priority.INTERACTIVE)
    service.start()
    try:
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
        time.sleep(0.05)
    finally:
        service.stop()
    # Two jobs were queued but only the authoritative one ran.
    assert len([call for call in gateway.calls if call[0] == "show_databases"]) == 1


def test_is_busy_reports_queued_work(repo) -> None:
    service = FetchService(FakeGateway(), repo)
    scope = databases_scope()
    assert service.is_busy(scope) is False
    service.request(scope)
    assert service.is_busy(scope) is True


# ---------------------------------------------------------------- priority


def test_interactive_work_is_dequeued_before_background_work(repo) -> None:
    """One job == one page is what makes this hold in practice: a long sweep
    yields at every page boundary instead of blocking the queue."""
    gateway = FakeGateway(
        databases=["ACME"],
        schemas={"A": ["S"], "B": ["S"], "C": ["S"]},
    )
    order: list[str] = []
    gate = threading.Event()

    class Gated(FakeGateway):
        def show_databases(self, **kwargs):
            gate.wait(timeout=2)
            return super().show_databases(**kwargs)

    gated = Gated(databases=gateway.databases, schemas=gateway.schemas)
    service = FetchService(
        gated, repo, workers=1, notify=lambda scope, complete: order.append(scope.key)
    )
    # Occupy the single worker so the rest of the queue has to sort itself out.
    service.request(databases_scope(), Priority.INTERACTIVE)
    service.start()
    try:
        assert wait_until(lambda: service.is_busy(databases_scope()))
        service.request(schemas_scope("A"), Priority.BACKGROUND)
        service.request(schemas_scope("B"), Priority.BACKGROUND)
        service.request(schemas_scope("C"), Priority.INTERACTIVE)
        gate.set()
        assert wait_until(lambda: len(order) == 4)
    finally:
        gate.set()
        service.stop()

    assert order[0] == "databases"
    assert order[1] == "schemas:C"
    assert set(order[2:]) == {"schemas:A", "schemas:B"}


def test_jobs_of_equal_priority_keep_their_arrival_order() -> None:
    first = FetchJob(priority=Priority.BACKGROUND, scope=databases_scope())
    second = FetchJob(priority=Priority.BACKGROUND, scope=schemas_scope("MYDB"))
    assert first < second


def test_priority_beats_arrival_order() -> None:
    early = FetchJob(priority=Priority.BACKGROUND, scope=databases_scope())
    late = FetchJob(priority=Priority.INTERACTIVE, scope=schemas_scope("MYDB"))
    assert late < early


# ------------------------------------------------------------ cancellation


def test_cancel_where_drops_queued_scopes(repo) -> None:
    """Switching database must not leave the old one's sweep hogging the pool."""
    service = FetchService(FakeGateway(), repo)
    service.request(schemas_scope("OLD"), Priority.BACKGROUND)
    service.request(schemas_scope("NEW"), Priority.INTERACTIVE)

    dropped = service.cancel_where(lambda scope: scope.database == "OLD")

    assert [scope.key for scope in dropped] == ["schemas:OLD"]
    assert service.is_busy(schemas_scope("OLD")) is False
    assert service.is_busy(schemas_scope("NEW")) is True


def test_a_cancelled_scope_stops_paging_after_the_current_page(repo) -> None:
    """The in-flight page cannot be interrupted -- a thread worker's blocking
    call is not preemptible -- but the chain must stop right after it."""
    gateway = FakeGateway(databases=[f"DB_{i:03d}" for i in range(100)])
    pages: list[bool] = []
    service = FetchService(
        gateway,
        repo,
        page_size=10,
        notify=lambda scope, complete: pages.append(complete),
    )
    scope = databases_scope()
    service.request(scope)
    service.start()
    try:
        assert wait_until(lambda: len(pages) >= 1)
        service.cancel_where(lambda s: s == scope)
        time.sleep(0.2)
    finally:
        service.stop()
    # Far short of the ten pages a full run would have produced.
    assert len(pages) < 10
    assert repo.entry(scope).state is CacheState.PARTIAL


def test_requesting_a_cancelled_scope_again_revives_it(repo) -> None:
    gateway = FakeGateway(databases=["ACME"])
    service = FetchService(gateway, repo)
    scope = databases_scope()
    service.request(scope)
    service.cancel_where(lambda s: s == scope)
    assert service.is_busy(scope) is False
    assert service.request(scope) is True


# -------------------------------------------------------------- lifecycle


def test_starting_twice_does_not_double_the_workers(repo) -> None:
    service = FetchService(FakeGateway(), repo)
    service.start()
    service.start()
    try:
        assert len(service._threads) == 1
    finally:
        service.stop()


def test_stopping_an_unstarted_service_is_harmless(repo) -> None:
    FetchService(FakeGateway(), repo).stop()


def test_objects_scopes_reach_the_right_gateway_call(repo) -> None:
    gateway = FakeGateway(objects={("MYDB", "PUBLIC", ObjectKind.PIPE): ["P1", "P2"]})
    scope = objects_scope(ObjectKind.PIPE, "MYDB", "PUBLIC")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [o.name for o in repo.objects(scope).rows] == ["P1", "P2"]


def test_stopping_with_work_still_queued_does_not_raise(repo) -> None:
    """Regression: the stop sentinel shares a PriorityQueue with real jobs, so
    it has to be orderable against them. A bare object() sentinel raised
    TypeError the moment the queue was not empty -- which is every real
    shutdown, and none of the unit tests that stopped an idle service."""
    gateway = FakeGateway(databases=[f"DB_{i:03d}" for i in range(200)])
    service = FetchService(gateway, repo, page_size=5)
    service.request(databases_scope(), Priority.BACKGROUND)
    service.request(schemas_scope("A"), Priority.BACKGROUND)
    service.request(schemas_scope("B"), Priority.INTERACTIVE)
    service.start()
    service.stop()


def test_the_shutdown_sentinel_sorts_ahead_of_real_work() -> None:
    """Stopping has to be prompt, not queued behind everything outstanding."""
    from snowcone.service.jobs import SHUTDOWN

    urgent = FetchJob(priority=Priority.INTERACTIVE, scope=databases_scope())
    assert SHUTDOWN < urgent
