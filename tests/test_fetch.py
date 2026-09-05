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


def test_warehouses_and_roles_are_fetched_in_one_shot() -> None:
    """Small, flat, unpaged lists -- like columns, not like the object mirrors."""
    from snowcone.models import roles_scope, warehouses_scope

    gateway = FakeGateway(warehouses=["COMPUTE_WH"], roles=["SYSADMIN", "PUBLIC"])
    rows, cursor = fetch_page(gateway, warehouses_scope(), None, limit=1)
    assert [row["name"] for row in rows] == ["COMPUTE_WH"]
    assert cursor is None

    rows, cursor = fetch_page(gateway, roles_scope(), None, limit=1)
    assert [row["name"] for row in rows] == ["PUBLIC", "SYSADMIN"]
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


# ------------------------------------------------------ database-wide prime


def prime_gateway() -> FakeGateway:
    return FakeGateway(
        schemas={"MYDB": ["AAA", "BBB", "EMPTY"]},
        objects={
            ("MYDB", "AAA", ObjectKind.TABLE): ["A1", "A2", "A3"],
            ("MYDB", "BBB", ObjectKind.TABLE): ["B1", "B2"],
        },
    )


def load_schemas_named(repo, gateway, database: str) -> None:
    scope = schemas_scope(database)
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, gateway.show_schemas(database), None)
    repo.finish_pass(scope, generation)


def load_schemas(repo, gateway) -> None:
    load_schemas_named(repo, gateway, "MYDB")


def test_one_prime_fills_every_schemas_scope(repo) -> None:
    """The measured win: one statement instead of a round trip per schema."""
    from snowcone.models import prime_scope

    gateway = prime_gateway()
    load_schemas(repo, gateway)
    service = make_service(gateway, repo)
    scope = prime_scope(ObjectKind.TABLE, "MYDB")
    try:
        service.request(scope)
        assert wait_until(
            lambda: (
                repo.entry(objects_scope(ObjectKind.TABLE, "MYDB", "BBB")).state
                is CacheState.COMPLETE
            )
        )
    finally:
        service.stop()

    aaa = objects_scope(ObjectKind.TABLE, "MYDB", "AAA")
    bbb = objects_scope(ObjectKind.TABLE, "MYDB", "BBB")
    assert [o.name for o in repo.objects(aaa).rows] == ["A1", "A2", "A3"]
    assert [o.name for o in repo.objects(bbb).rows] == ["B1", "B2"]


def test_a_prime_costs_one_round_trip_not_one_per_schema(repo) -> None:
    from snowcone.models import prime_scope

    gateway = prime_gateway()
    load_schemas(repo, gateway)
    gateway.calls.clear()
    service = make_service(gateway, repo)
    try:
        service.request(prime_scope(ObjectKind.TABLE, "MYDB"))
        assert wait_until(
            lambda: (
                repo.entry(objects_scope(ObjectKind.TABLE, "MYDB", "AAA")).state
                is CacheState.COMPLETE
            )
        )
    finally:
        service.stop()
    per_schema = [c for c in gateway.calls if c[0] == "show_objects"]
    assert per_schema == []
    assert len([c for c in gateway.calls if c[0] == "show_objects_in_database"]) == 1


def test_a_schema_with_no_objects_still_completes_a_pass(repo) -> None:
    """Otherwise the node would read as 'never fetched' and the tree would
    keep asking for it, so it would never settle."""
    from snowcone.models import prime_scope

    gateway = prime_gateway()
    load_schemas(repo, gateway)
    service = make_service(gateway, repo)
    empty = objects_scope(ObjectKind.TABLE, "MYDB", "EMPTY")
    try:
        service.request(prime_scope(ObjectKind.TABLE, "MYDB"))
        assert wait_until(lambda: repo.entry(empty).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert repo.objects(empty).rows == []
    assert repo.objects(empty).age() is not None


def test_a_prime_announces_each_schema_it_filled(repo) -> None:
    """The tree routes updates by scope, so a prime has to name the scopes it
    touched rather than only itself."""
    from snowcone.models import prime_scope

    gateway = prime_gateway()
    load_schemas(repo, gateway)
    seen: list[str] = []
    service = make_service(
        gateway, repo, notify=lambda scope, complete: seen.append(scope.key)
    )
    try:
        service.request(prime_scope(ObjectKind.TABLE, "MYDB"))
        assert wait_until(lambda: "tables@MYDB" in seen)
    finally:
        service.stop()
    assert "tables:MYDB.AAA" in seen
    assert "tables:MYDB.BBB" in seen
    assert "tables:MYDB.EMPTY" in seen


def test_duplicate_names_across_schemas_are_all_kept(repo) -> None:
    """Regression: object names repeat across schemas, so keyset paging by
    name across a whole database silently drops every duplicate after the
    first. The prime therefore fetches unpaged."""
    from snowcone.models import prime_scope

    schemas = [f"S{i}" for i in range(5)]
    gateway = FakeGateway(
        schemas={"MYDB": schemas},
        # The same three names in every schema -- the case that lost rows.
        objects={
            ("MYDB", schema, ObjectKind.TABLE): ["ORDERS", "CUSTOMERS", "ITEMS"]
            for schema in schemas
        },
    )
    load_schemas_named(repo, gateway, "MYDB")
    service = make_service(gateway, repo)
    try:
        service.request(prime_scope(ObjectKind.TABLE, "MYDB"))
        assert wait_until(
            lambda: all(
                repo.entry(objects_scope(ObjectKind.TABLE, "MYDB", s)).state
                is CacheState.COMPLETE
                for s in schemas
            )
        )
    finally:
        service.stop()
    total = sum(
        repo.child_count(objects_scope(ObjectKind.TABLE, "MYDB", s)) for s in schemas
    )
    assert total == 15


def test_a_result_at_the_show_cap_falls_back_to_per_schema(repo) -> None:
    """At the cap the result is truncated and cannot be safely resumed, so
    correctness wins over the round-trip saving."""
    from snowcone.models import prime_scope

    schemas = ["AAA", "BBB"]
    gateway = FakeGateway(
        schemas={"MYDB": schemas},
        objects={
            ("MYDB", "AAA", ObjectKind.TABLE): [f"T{i:05d}" for i in range(6000)],
            ("MYDB", "BBB", ObjectKind.TABLE): [f"U{i:05d}" for i in range(6000)],
        },
    )
    load_schemas_named(repo, gateway, "MYDB")
    service = make_service(gateway, repo, workers=2)
    try:
        service.request(prime_scope(ObjectKind.TABLE, "MYDB"))
        assert wait_until(
            lambda: all(
                repo.entry(objects_scope(ObjectKind.TABLE, "MYDB", s)).state
                is CacheState.COMPLETE
                for s in schemas
            ),
            timeout=10,
        )
    finally:
        service.stop()
    assert repo.child_count(objects_scope(ObjectKind.TABLE, "MYDB", "AAA")) == 6000
    assert repo.child_count(objects_scope(ObjectKind.TABLE, "MYDB", "BBB")) == 6000
    assert any(call[0] == "show_objects" for call in gateway.calls)


def test_a_prime_scope_has_no_mirror_table_of_its_own(repo) -> None:
    """It is a fetch strategy, not a set of rows."""
    import pytest as _pytest

    from snowcone.cache.repository import spec_for
    from snowcone.models import prime_scope

    with _pytest.raises(ValueError):
        spec_for(prime_scope(ObjectKind.TABLE, "MYDB"))


def test_a_failing_prime_is_reported(repo) -> None:
    from snowcone.models import prime_scope

    class Boom(FakeGateway):
        def show_objects_in_database(self, *args, **kwargs):
            raise RuntimeError("no access")

    errors: list[str] = []
    service = make_service(
        Boom(), repo, on_error=lambda scope, message: errors.append(message)
    )
    try:
        service.request(prime_scope(ObjectKind.TABLE, "MYDB"))
        assert wait_until(lambda: bool(errors))
    finally:
        service.stop()
    assert errors == ["no access"]


def test_columns_are_fetched_without_a_warehouse(repo) -> None:
    """SHOW COLUMNS, not INFORMATION_SCHEMA: the verified account has no
    warehouse, and a SELECT would fail there."""
    from snowcone.models import columns_scope

    gateway = FakeGateway(columns={("MYDB", "PUBLIC", "ORDERS"): ["ID", "TOTAL"]})
    scope = columns_scope("MYDB", "PUBLIC", "ORDERS")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [c.column_name for c in repo.columns(scope).rows] == ["ID", "TOTAL"]
    assert [c[0] for c in gateway.calls] == ["show_columns"]
