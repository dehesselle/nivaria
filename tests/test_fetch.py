from __future__ import annotations

import threading
import time

import pytest

from nivaria.cache.db import Database
from nivaria.cache.repository import Repository
from nivaria.models import (
    CacheState,
    ObjectType,
    columns_scope,
    databases_scope,
    grants_scope,
    objects_scope,
    schemas_scope,
    tag_references_scope,
)
from nivaria.service.fetch import FetchService
from nivaria.service.jobs import FetchJob, Priority, fetch_page
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
    page = fetch_page(gateway, databases_scope(), None, limit=10)
    assert len(page.rows) == 2
    assert page.cursor is None


def test_a_full_page_yields_the_last_name_as_the_cursor() -> None:
    gateway = FakeGateway(databases=["ACME", "BETA", "GAMMA"])
    page = fetch_page(gateway, databases_scope(), None, limit=2)
    assert [row["name"] for row in page.rows] == ["ACME", "BETA"]
    assert page.cursor == "BETA"


def test_an_empty_result_ends_the_chain() -> None:
    page = fetch_page(FakeGateway(), databases_scope(), None, limit=10)
    assert page.rows == []
    assert page.cursor is None


def test_columns_are_fetched_in_one_shot() -> None:
    """A single table's columns are small and bounded, so paging them would
    only add round trips."""
    gateway = FakeGateway(columns={("MYDB", "PUBLIC", "ORDERS"): ["ID", "TOTAL"]})
    from nivaria.models import columns_scope

    page = fetch_page(gateway, columns_scope("MYDB", "PUBLIC", "ORDERS"), None, limit=1)
    assert len(page.rows) == 2
    assert page.cursor is None


def test_warehouses_and_roles_are_fetched_in_one_shot() -> None:
    """Small, flat, unpaged lists -- like columns, not like the object mirrors."""
    from nivaria.models import roles_scope, warehouses_scope

    gateway = FakeGateway(warehouses=["COMPUTE_WH"], roles=["SYSADMIN", "PUBLIC"])
    page = fetch_page(gateway, warehouses_scope(), None, limit=1)
    assert [row["name"] for row in page.rows] == ["COMPUTE_WH"]
    assert page.cursor is None

    page = fetch_page(gateway, roles_scope(), None, limit=1)
    assert [row["name"] for row in page.rows] == ["PUBLIC", "SYSADMIN"]
    assert page.cursor is None


def test_table_stats_is_fetched_in_one_shot() -> None:
    """A schema's table count is bounded, like columns -- no paging needed."""
    from nivaria.models import table_stats_scope

    gateway = FakeGateway(
        table_stats={
            ("MYDB", "PUBLIC"): [
                {
                    "TABLE_CATALOG": "MYDB",
                    "TABLE_SCHEMA": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                    "LAST_DDL": "2026-01-01",
                    "LAST_ALTERED": "2026-01-02",
                }
            ]
        }
    )
    page = fetch_page(gateway, table_stats_scope("MYDB", "PUBLIC"), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None


def test_view_stats_is_fetched_in_one_shot() -> None:
    """A schema's view count is bounded, same reasoning as table_stats."""
    from nivaria.models import view_stats_scope

    gateway = FakeGateway(
        view_stats={
            ("MYDB", "PUBLIC"): [
                {
                    "TABLE_CATALOG": "MYDB",
                    "TABLE_SCHEMA": "PUBLIC",
                    "TABLE_NAME": "ORDERS_V",
                    "LAST_DDL": "2026-01-01",
                    "LAST_ALTERED": "2026-01-02",
                }
            ]
        }
    )
    page = fetch_page(gateway, view_stats_scope("MYDB", "PUBLIC"), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None


def test_materialized_view_stats_is_fetched_in_one_shot() -> None:
    """A schema's materialized-view count is bounded, same reasoning as
    table_stats/view_stats."""
    from nivaria.models import materialized_view_stats_scope

    gateway = FakeGateway(
        materialized_view_stats={
            ("MYDB", "PUBLIC"): [
                {
                    "database_name": "MYDB",
                    "schema_name": "PUBLIC",
                    "name": "TOTALS_MV",
                    "rows": 42,
                    "bytes": 8192,
                }
            ]
        }
    )
    page = fetch_page(
        gateway, materialized_view_stats_scope("MYDB", "PUBLIC"), None, limit=1
    )
    assert len(page.rows) == 1
    assert page.cursor is None


def test_schema_level_grants_are_fetched_in_one_shot() -> None:
    """A schema's grant list is bounded, like table_stats -- no paging needed,
    and a schema scope dispatches to show_grants_on_schema, not the
    database-level call."""
    from nivaria.models import grants_scope

    gateway = FakeGateway(
        grants_schema={
            ("MYDB", "PUBLIC"): [
                {"privilege": "USAGE", "granted_to": "ROLE", "grantee_name": "ANALYST"}
            ]
        }
    )
    page = fetch_page(gateway, grants_scope("MYDB", "PUBLIC"), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None
    assert [c[0] for c in gateway.calls] == ["show_grants_on_schema"]


def test_an_objects_grants_are_fetched_in_one_shot() -> None:
    """A scope naming one object dispatches to show_grants_on_object, with
    that object's type."""
    from nivaria.models import ObjectRef, ObjectType, object_grants_scope

    gateway = FakeGateway(
        grants_object={
            (ObjectType.TABLE, "MYDB", "PUBLIC", "ORDERS"): [
                {"privilege": "SELECT", "granted_to": "ROLE", "grantee_name": "ANALYST"}
            ]
        }
    )
    ref = ObjectRef(ObjectType.TABLE, "MYDB", "PUBLIC", "ORDERS")
    page = fetch_page(gateway, object_grants_scope(ref), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None
    assert gateway.calls == [
        ("show_grants_on_object", (ObjectType.TABLE, "MYDB", "PUBLIC", "ORDERS"))
    ]


def test_database_level_grants_are_fetched_in_one_shot() -> None:
    """schema=None on the scope means database-level -- dispatches to
    show_grants_on_database instead."""
    from nivaria.models import grants_scope

    gateway = FakeGateway(
        grants_database={
            "MYDB": [
                {"privilege": "USAGE", "granted_to": "ROLE", "grantee_name": "ANALYST"}
            ]
        }
    )
    page = fetch_page(gateway, grants_scope("MYDB"), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None
    assert [c[0] for c in gateway.calls] == ["show_grants_on_database"]


def test_schema_level_tag_references_are_fetched_in_one_shot() -> None:
    from nivaria.models import tag_references_scope

    gateway = FakeGateway(
        tag_references_schema={
            ("MYDB", "PUBLIC"): [
                {"TAG_NAME": "COST_CENTER", "TAG_VALUE": "ENG", "DOMAIN": "SCHEMA"}
            ]
        }
    )
    page = fetch_page(gateway, tag_references_scope("MYDB", "PUBLIC"), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None
    assert [c[0] for c in gateway.calls] == ["tag_references_schema"]


def test_database_level_tag_references_are_fetched_in_one_shot() -> None:
    from nivaria.models import tag_references_scope

    gateway = FakeGateway(
        tag_references_database={
            "MYDB": [
                {"TAG_NAME": "COST_CENTER", "TAG_VALUE": "ENG", "DOMAIN": "DATABASE"}
            ]
        }
    )
    page = fetch_page(gateway, tag_references_scope("MYDB"), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None
    assert [c[0] for c in gateway.calls] == ["tag_references_database"]


def test_pipe_activity_is_fetched_in_one_shot() -> None:
    """A schema's pipe count is bounded, same reasoning as table_stats."""
    from nivaria.models import pipe_activity_scope

    gateway = FakeGateway(
        pipe_activity={
            ("MYDB", "PUBLIC"): [
                {
                    "PIPE_CATALOG_NAME": "MYDB",
                    "PIPE_SCHEMA_NAME": "PUBLIC",
                    "PIPE_NAME": "MY_PIPE",
                    "FILE_NAME": "orders_1.csv.gz",
                    "FILE_SIZE": 1024,
                    "ROW_COUNT": 42,
                    "STATUS": "Loaded",
                    "LAST_LOAD_TIME": "2026-01-02T03:04:05+00:00",
                    "TABLE_CATALOG_NAME": "MYDB",
                    "TABLE_SCHEMA_NAME": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                }
            ]
        }
    )
    page = fetch_page(gateway, pipe_activity_scope("MYDB", "PUBLIC"), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None


def test_pipe_activity_history_is_fetched_in_one_shot() -> None:
    """One pipe's full backfill is likewise bounded and unpaged -- no
    pipe_target needed, unlike pipe_history below: ACCOUNT_USAGE.COPY_HISTORY
    filters by PIPE_NAME directly."""
    from nivaria.models import pipe_activity_history_scope

    gateway = FakeGateway(
        pipe_activity_history={
            ("MYDB", "PUBLIC", "MY_PIPE"): [
                {
                    "PIPE_CATALOG_NAME": "MYDB",
                    "PIPE_SCHEMA_NAME": "PUBLIC",
                    "PIPE_NAME": "MY_PIPE",
                    "FILE_NAME": "orders_1.csv.gz",
                    "FILE_SIZE": 1024,
                    "ROW_COUNT": 42,
                    "STATUS": "Loaded",
                    "LAST_LOAD_TIME": "2026-01-02T03:04:05+00:00",
                    "TABLE_CATALOG_NAME": "MYDB",
                    "TABLE_SCHEMA_NAME": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                }
            ]
        }
    )
    page = fetch_page(
        gateway, pipe_activity_history_scope("MYDB", "PUBLIC", "MY_PIPE"), None, limit=1
    )
    assert len(page.rows) == 1
    assert page.cursor is None


def test_pipe_history_is_fetched_in_one_shot_given_its_resolved_target() -> None:
    """One pipe's near-real-time history is likewise bounded and unpaged --
    same reasoning as pipe_activity above."""
    from nivaria.models import pipe_history_scope

    gateway = FakeGateway(
        pipe_history={
            ("MYDB", "PUBLIC", "ORDERS", "LOAD_ORDERS"): [
                {
                    "FILE_NAME": "orders_9.csv.gz",
                    "FILE_SIZE": 2048,
                    "ROW_COUNT": 100,
                    "STATUS": "Loaded",
                    "LAST_LOAD_TIME": "2026-03-01T08:00:00+00:00",
                    "PIPE_NAME": "LOAD_ORDERS",
                }
            ]
        }
    )
    page = fetch_page(
        gateway,
        pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
        None,
        limit=1,
        pipe_target=("MYDB", "PUBLIC", "ORDERS"),
    )
    assert len(page.rows) == 1
    assert page.cursor is None


def test_pipe_history_without_a_resolved_target_raises() -> None:
    """fetch_page has no repository access and so cannot resolve the target
    table itself -- see FetchService._resolve_pipe_target, the caller that
    is supposed to supply `pipe_target`. A caller that forgets is a bug, not
    a silent no-op, so this raises rather than fetching nothing quietly."""
    from nivaria.models import pipe_history_scope

    gateway = FakeGateway()
    with pytest.raises(ValueError):
        fetch_page(
            gateway,
            pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS"),
            None,
            limit=1,
        )


def test_task_activity_is_fetched_in_one_shot() -> None:
    """A schema's task count is bounded, same reasoning as pipe_activity."""
    from nivaria.models import task_activity_scope

    gateway = FakeGateway(
        task_activity={
            ("MYDB", "PUBLIC"): [
                {
                    "NAME": "REFRESH_ORDERS",
                    "DATABASE_NAME": "MYDB",
                    "SCHEMA_NAME": "PUBLIC",
                    "QUERY_ID": "01ab-query-id",
                    "SCHEDULED_TIME": "2026-01-02T03:04:05+00:00",
                    "STATE": "SUCCEEDED",
                }
            ]
        }
    )
    page = fetch_page(gateway, task_activity_scope("MYDB", "PUBLIC"), None, limit=1)
    assert len(page.rows) == 1
    assert page.cursor is None


def test_task_activity_history_is_fetched_in_one_shot() -> None:
    """One task's full backfill is likewise bounded and unpaged -- no
    resolution needed, same reasoning as pipe_activity_history."""
    from nivaria.models import task_activity_history_scope

    gateway = FakeGateway(
        task_activity_history={
            ("MYDB", "PUBLIC", "REFRESH_ORDERS"): [
                {
                    "NAME": "REFRESH_ORDERS",
                    "DATABASE_NAME": "MYDB",
                    "SCHEMA_NAME": "PUBLIC",
                    "QUERY_ID": "01ab-query-id",
                    "SCHEDULED_TIME": "2026-01-02T03:04:05+00:00",
                    "STATE": "SUCCEEDED",
                }
            ]
        }
    )
    page = fetch_page(
        gateway,
        task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS"),
        None,
        limit=1,
    )
    assert len(page.rows) == 1
    assert page.cursor is None


def test_task_history_is_fetched_in_one_shot_with_no_target_resolution() -> None:
    """Unlike pipe_history, this needs no `pipe_target`-style extra parameter
    at all: DATABASE_NAME/SCHEMA_NAME/TASK_NAME come straight off the scope,
    so calling fetch_page with nothing extra must simply succeed."""
    from nivaria.models import task_history_scope

    gateway = FakeGateway(
        task_history={
            ("MYDB", "PUBLIC", "REFRESH_ORDERS"): [
                {
                    "NAME": "REFRESH_ORDERS",
                    "QUERY_ID": "01ab-query-id",
                    "SCHEDULED_TIME": "2026-03-01T08:00:00+00:00",
                    "STATE": "SUCCEEDED",
                }
            ]
        }
    )
    page = fetch_page(
        gateway,
        task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS"),
        None,
        limit=1,
    )
    assert len(page.rows) == 1
    assert page.cursor is None


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
        Boom(),
        repo,
        on_error=lambda s, message, _missing: errors.append((s.key, message)),
    )
    try:
        service.request(scope)
        assert wait_until(lambda: bool(errors))
    finally:
        service.stop()

    assert errors[0] == ("databases", "network down")
    assert repo.entry(scope).state is CacheState.ERROR
    assert [d.name for d in repo.databases(scope).rows] == ["ACME"]


def cache_schemas(repo, database: str, *names: str) -> None:
    scope = schemas_scope(database)
    generation = repo.begin_pass(scope)
    rows = [{"name": name, "database_name": database} for name in names]
    repo.write_page(scope, generation, rows, None)
    repo.finish_pass(scope, generation)


def cached_schema_names(repo, database: str) -> list[str]:
    return [row.name for row in repo.schemas(schemas_scope(database)).rows]


def test_a_schema_dropped_elsewhere_leaves_the_cache_once_a_fetch_finds_it_gone(
    repo,
) -> None:
    """Visiting a dropped schema used to fail and leave it listed until the
    TTL sweep; the failure itself now refreshes the list it came from."""
    gateway = FakeGateway(schemas={"MYDB": ["AAA", "GONE"]})
    cache_schemas(repo, "MYDB", "AAA", "GONE")
    gateway.drop_schema("MYDB", "GONE")
    errors: list[tuple[str, bool]] = []
    service = make_service(
        gateway,
        repo,
        on_error=lambda s, message, missing: errors.append((s.key, missing)),
    )
    try:
        service.request(grants_scope("MYDB", "GONE"))
        assert wait_until(lambda: cached_schema_names(repo, "MYDB") == ["AAA"])
    finally:
        service.stop()
    assert errors == [("grants:MYDB.GONE", True)]


def test_a_failure_that_is_not_a_missing_object_refreshes_nothing_else(
    repo,
) -> None:
    """A session without a warehouse fails every SELECT-backed scope on every
    visit; that must not also cost a SHOW SCHEMAS each time."""
    gateway = FakeGateway(schemas={"MYDB": ["AAA"]})
    gateway.raise_on_tag_references = True
    errors: list[bool] = []
    service = make_service(
        gateway, repo, on_error=lambda s, message, missing: errors.append(missing)
    )
    try:
        service.request(tag_references_scope("MYDB", "AAA"))
        assert wait_until(lambda: bool(errors))
        time.sleep(0.05)
    finally:
        service.stop()
    assert errors == [False]
    assert [name for name, _ in gateway.calls] == ["tag_references_schema"]


def test_a_dropped_schema_found_one_level_further_down_is_still_cleared(
    repo,
) -> None:
    """A table's columns name the table, not the schema, so the refresh
    climbs: the schema's own table listing fails the same way, and that
    failure refreshes the schema list."""
    gateway = FakeGateway(schemas={"MYDB": ["AAA", "GONE"]})
    cache_schemas(repo, "MYDB", "AAA", "GONE")
    gateway.drop_schema("MYDB", "GONE")
    service = make_service(gateway, repo)
    try:
        service.request(columns_scope("MYDB", "GONE", "ORDERS"))
        assert wait_until(lambda: cached_schema_names(repo, "MYDB") == ["AAA"])
    finally:
        service.stop()


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
    gateway = FakeGateway(objects={("MYDB", "PUBLIC", ObjectType.PIPE): ["P1", "P2"]})
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
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


class _StragglingGateway(FakeGateway):
    """A SHOW DATABASES that is still running when the service is stopped,
    and returns -- or fails -- only once the test lets it."""

    def __init__(self, *, error: Exception | None = None) -> None:
        super().__init__(databases=["ACME"])
        self.entered = threading.Event()
        self.release = threading.Event()
        self._error = error

    def show_databases(self, *, after=None, limit=1000, allow_reconnect=True):
        self.entered.set()
        assert self.release.wait(timeout=3.0)
        if self._error is not None:
            raise self._error
        return super().show_databases(
            after=after, limit=limit, allow_reconnect=allow_reconnect
        )


def _straggle(repo, gateway: _StragglingGateway) -> tuple[list, list]:
    """Stop the service while its one worker is mid-query, then let the
    query finish. Returns what reached notify and on_error."""
    notified: list = []
    reported: list = []
    service = make_service(
        gateway,
        repo,
        notify=lambda *args: notified.append(args),
        on_error=lambda *args: reported.append(args),
    )
    service.request(databases_scope())
    assert gateway.entered.wait(timeout=3.0)
    workers = list(service._threads)
    service.stop(timeout=0.05)  # gives up on the blocked worker
    gateway.release.set()
    for worker in workers:
        worker.join(timeout=3.0)
    return notified, reported


def test_a_worker_still_mid_query_when_the_service_stops_reports_nothing(
    repo,
) -> None:
    """Regression: a QA query that outlived stop() on a profile switch failed
    against the just-closed QA pool and toasted its error into the PROD
    session the user had already switched to."""
    gateway = _StragglingGateway(error=RuntimeError("connection pool is closed"))
    notified, reported = _straggle(repo, gateway)
    assert notified == []
    assert reported == []
    assert repo.entry(databases_scope()).error is None


def test_a_worker_still_mid_query_when_the_service_stops_writes_nothing(
    repo,
) -> None:
    """On a role switch the pool is shared, so a straggler's retry would run
    under the new role -- what it brings back cannot be trusted to belong to
    the cache it would be written to."""
    notified, _ = _straggle(repo, _StragglingGateway())
    assert notified == []
    assert repo.databases(databases_scope()).is_empty
    assert repo.entry(databases_scope()).state is not CacheState.COMPLETE


def test_a_stopped_service_counts_its_query_in_flight_until_it_returns(
    repo,
) -> None:
    """Quitting waits on running_count, not on a join, so the count has to
    outlive stop() for exactly as long as the statement does."""
    gateway = _StragglingGateway()
    service = make_service(gateway, repo)
    service.request(databases_scope())
    assert gateway.entered.wait(timeout=3.0)
    service.stop(timeout=0)
    assert service.running_count == 1
    gateway.release.set()
    assert wait_until(lambda: service.running_count == 0)


def test_a_stopped_service_starts_no_queued_job(repo) -> None:
    """A worker can dequeue real work just before stop() queues its
    SHUTDOWN. Running it would issue a statement whose result belongs to
    nobody, and keep quit waiting on it."""
    service = FetchService(FakeGateway(databases=["ACME"]), repo)
    service.request(databases_scope())
    job = service._pending[databases_scope()]
    service.stop()
    assert service._claim(job) is False
    assert service.running_count == 0


def test_the_shutdown_sentinel_sorts_ahead_of_real_work() -> None:
    """Stopping has to be prompt, not queued behind everything outstanding."""
    from nivaria.service.jobs import SHUTDOWN

    urgent = FetchJob(priority=Priority.INTERACTIVE, scope=databases_scope())
    assert SHUTDOWN < urgent


# ------------------------------------------------------ database-wide prime


def prime_gateway() -> FakeGateway:
    return FakeGateway(
        schemas={"MYDB": ["AAA", "BBB", "EMPTY"]},
        objects={
            ("MYDB", "AAA", ObjectType.TABLE): ["A1", "A2", "A3"],
            ("MYDB", "BBB", ObjectType.TABLE): ["B1", "B2"],
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
    from nivaria.models import prime_scope

    gateway = prime_gateway()
    load_schemas(repo, gateway)
    service = make_service(gateway, repo)
    scope = prime_scope(ObjectType.TABLE, "MYDB")
    try:
        service.request(scope)
        assert wait_until(
            lambda: (
                repo.entry(objects_scope(ObjectType.TABLE, "MYDB", "BBB")).state
                is CacheState.COMPLETE
            )
        )
    finally:
        service.stop()

    aaa = objects_scope(ObjectType.TABLE, "MYDB", "AAA")
    bbb = objects_scope(ObjectType.TABLE, "MYDB", "BBB")
    assert [o.name for o in repo.objects(aaa).rows] == ["A1", "A2", "A3"]
    assert [o.name for o in repo.objects(bbb).rows] == ["B1", "B2"]


def test_a_prime_costs_one_round_trip_not_one_per_schema(repo) -> None:
    from nivaria.models import prime_scope

    gateway = prime_gateway()
    load_schemas(repo, gateway)
    gateway.calls.clear()
    service = make_service(gateway, repo)
    try:
        service.request(prime_scope(ObjectType.TABLE, "MYDB"))
        assert wait_until(
            lambda: (
                repo.entry(objects_scope(ObjectType.TABLE, "MYDB", "AAA")).state
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
    from nivaria.models import prime_scope

    gateway = prime_gateway()
    load_schemas(repo, gateway)
    service = make_service(gateway, repo)
    empty = objects_scope(ObjectType.TABLE, "MYDB", "EMPTY")
    try:
        service.request(prime_scope(ObjectType.TABLE, "MYDB"))
        assert wait_until(lambda: repo.entry(empty).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert repo.objects(empty).rows == []
    assert repo.objects(empty).age() is not None


def test_a_prime_announces_each_schema_it_filled(repo) -> None:
    """The tree routes updates by scope, so a prime has to name the scopes it
    touched rather than only itself."""
    from nivaria.models import prime_scope

    gateway = prime_gateway()
    load_schemas(repo, gateway)
    seen: list[str] = []
    service = make_service(
        gateway, repo, notify=lambda scope, complete: seen.append(scope.key)
    )
    try:
        service.request(prime_scope(ObjectType.TABLE, "MYDB"))
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
    from nivaria.models import prime_scope

    schemas = [f"S{i}" for i in range(5)]
    gateway = FakeGateway(
        schemas={"MYDB": schemas},
        # The same three names in every schema -- the case that lost rows.
        objects={
            ("MYDB", schema, ObjectType.TABLE): ["ORDERS", "CUSTOMERS", "ITEMS"]
            for schema in schemas
        },
    )
    load_schemas_named(repo, gateway, "MYDB")
    service = make_service(gateway, repo)
    try:
        service.request(prime_scope(ObjectType.TABLE, "MYDB"))
        assert wait_until(
            lambda: all(
                repo.entry(objects_scope(ObjectType.TABLE, "MYDB", s)).state
                is CacheState.COMPLETE
                for s in schemas
            )
        )
    finally:
        service.stop()
    total = sum(
        repo.child_count(objects_scope(ObjectType.TABLE, "MYDB", s)) for s in schemas
    )
    assert total == 15


def test_a_result_at_the_show_cap_falls_back_to_per_schema(repo) -> None:
    """At the cap the result is truncated and cannot be safely resumed, so
    correctness wins over the round-trip saving."""
    from nivaria.models import prime_scope

    schemas = ["AAA", "BBB"]
    gateway = FakeGateway(
        schemas={"MYDB": schemas},
        objects={
            ("MYDB", "AAA", ObjectType.TABLE): [f"T{i:05d}" for i in range(6000)],
            ("MYDB", "BBB", ObjectType.TABLE): [f"U{i:05d}" for i in range(6000)],
        },
    )
    load_schemas_named(repo, gateway, "MYDB")
    service = make_service(gateway, repo, workers=2)
    try:
        service.request(prime_scope(ObjectType.TABLE, "MYDB"))
        assert wait_until(
            lambda: all(
                repo.entry(objects_scope(ObjectType.TABLE, "MYDB", s)).state
                is CacheState.COMPLETE
                for s in schemas
            ),
            timeout=10,
        )
    finally:
        service.stop()
    assert repo.child_count(objects_scope(ObjectType.TABLE, "MYDB", "AAA")) == 6000
    assert repo.child_count(objects_scope(ObjectType.TABLE, "MYDB", "BBB")) == 6000
    assert any(call[0] == "show_objects" for call in gateway.calls)


def test_a_prime_scope_has_no_mirror_table_of_its_own(repo) -> None:
    """It is a fetch strategy, not a set of rows."""
    import pytest as _pytest

    from nivaria.cache.repository import spec_for
    from nivaria.models import prime_scope

    with _pytest.raises(ValueError):
        spec_for(prime_scope(ObjectType.TABLE, "MYDB"))


def test_a_failing_prime_is_reported(repo) -> None:
    from nivaria.models import prime_scope

    class Boom(FakeGateway):
        def show_objects_in_database(self, *args, **kwargs):
            raise RuntimeError("no access")

    errors: list[str] = []
    service = make_service(
        Boom(), repo, on_error=lambda scope, message, _missing: errors.append(message)
    )
    try:
        service.request(prime_scope(ObjectType.TABLE, "MYDB"))
        assert wait_until(lambda: bool(errors))
    finally:
        service.stop()
    assert errors == ["no access"]


def test_columns_are_fetched_without_a_warehouse(repo) -> None:
    """SHOW COLUMNS, not INFORMATION_SCHEMA: the verified account has no
    warehouse, and a SELECT would fail there."""
    from nivaria.models import columns_scope

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


def test_table_stats_scope_reaches_the_right_gateway_call(repo) -> None:
    from nivaria.models import table_stats_scope

    gateway = FakeGateway(
        table_stats={
            ("MYDB", "PUBLIC"): [
                {
                    "TABLE_CATALOG": "MYDB",
                    "TABLE_SCHEMA": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                    "LAST_DDL": "2026-01-01",
                    "LAST_ALTERED": "2026-01-02",
                }
            ]
        }
    )
    scope = table_stats_scope("MYDB", "PUBLIC")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [row["TABLE_NAME"] for row in repo.raw_rows(scope)] == ["ORDERS"]
    assert [c[0] for c in gateway.calls] == ["table_stats"]


def test_view_stats_scope_reaches_the_right_gateway_call(repo) -> None:
    from nivaria.models import view_stats_scope

    gateway = FakeGateway(
        view_stats={
            ("MYDB", "PUBLIC"): [
                {
                    "TABLE_CATALOG": "MYDB",
                    "TABLE_SCHEMA": "PUBLIC",
                    "TABLE_NAME": "ORDERS_V",
                    "LAST_DDL": "2026-01-01",
                    "LAST_ALTERED": "2026-01-02",
                }
            ]
        }
    )
    scope = view_stats_scope("MYDB", "PUBLIC")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [row["TABLE_NAME"] for row in repo.raw_rows(scope)] == ["ORDERS_V"]
    assert [c[0] for c in gateway.calls] == ["view_stats"]


def test_materialized_view_stats_scope_reaches_the_right_gateway_call(repo) -> None:
    from nivaria.models import materialized_view_stats_scope

    gateway = FakeGateway(
        materialized_view_stats={
            ("MYDB", "PUBLIC"): [
                {
                    "database_name": "MYDB",
                    "schema_name": "PUBLIC",
                    "name": "TOTALS_MV",
                    "rows": 42,
                    "bytes": 8192,
                }
            ]
        }
    )
    scope = materialized_view_stats_scope("MYDB", "PUBLIC")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [row["name"] for row in repo.raw_rows(scope)] == ["TOTALS_MV"]
    assert [c[0] for c in gateway.calls] == ["materialized_view_stats"]


def test_a_failing_table_stats_fetch_keeps_previously_cached_stats(repo) -> None:
    """ "A failed pass does not sweep" for this scope specifically -- a session
    that loses its warehouse mid-browse must not blank out timestamps that
    were already fetched successfully."""
    from nivaria.models import table_stats_scope

    gateway = FakeGateway(
        table_stats={
            ("MYDB", "PUBLIC"): [
                {
                    "TABLE_CATALOG": "MYDB",
                    "TABLE_SCHEMA": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                    "LAST_DDL": "2026-01-01",
                    "LAST_ALTERED": "2026-01-02",
                }
            ]
        }
    )
    scope = table_stats_scope("MYDB", "PUBLIC")
    errors: list[str] = []
    service = make_service(
        gateway, repo, on_error=lambda s, message, _missing: errors.append(message)
    )
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)

        gateway.raise_on_table_stats = True
        service.request(scope, Priority.INTERACTIVE)
        assert wait_until(lambda: bool(errors))
    finally:
        service.stop()
    assert repo.entry(scope).state is CacheState.ERROR
    assert [row["TABLE_NAME"] for row in repo.raw_rows(scope)] == ["ORDERS"]


def test_pipe_activity_scope_reaches_the_right_gateway_call(repo) -> None:
    from nivaria.models import pipe_activity_scope

    gateway = FakeGateway(
        pipe_activity={
            ("MYDB", "PUBLIC"): [
                {
                    "PIPE_CATALOG_NAME": "MYDB",
                    "PIPE_SCHEMA_NAME": "PUBLIC",
                    "PIPE_NAME": "MY_PIPE",
                    "FILE_NAME": "orders_1.csv.gz",
                    "FILE_SIZE": 1024,
                    "ROW_COUNT": 42,
                    "STATUS": "Loaded",
                    "LAST_LOAD_TIME": "2026-01-02T03:04:05+00:00",
                    "TABLE_CATALOG_NAME": "MYDB",
                    "TABLE_SCHEMA_NAME": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                }
            ]
        }
    )
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [row["PIPE_NAME"] for row in repo.raw_rows(scope)] == ["MY_PIPE"]
    assert [c[0] for c in gateway.calls] == ["pipe_activity"]


def test_a_failing_pipe_activity_fetch_keeps_previously_cached_activity(repo) -> None:
    """ "A failed pass does not sweep" for this scope specifically -- losing
    the ACCOUNT_USAGE grant or the warehouse mid-browse must not blank out
    activity that was already fetched successfully."""
    from nivaria.models import pipe_activity_scope

    gateway = FakeGateway(
        pipe_activity={
            ("MYDB", "PUBLIC"): [
                {
                    "PIPE_CATALOG_NAME": "MYDB",
                    "PIPE_SCHEMA_NAME": "PUBLIC",
                    "PIPE_NAME": "MY_PIPE",
                    "FILE_NAME": "orders_1.csv.gz",
                    "FILE_SIZE": 1024,
                    "ROW_COUNT": 42,
                    "STATUS": "Loaded",
                    "LAST_LOAD_TIME": "2026-01-02T03:04:05+00:00",
                    "TABLE_CATALOG_NAME": "MYDB",
                    "TABLE_SCHEMA_NAME": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                }
            ]
        }
    )
    scope = pipe_activity_scope("MYDB", "PUBLIC")
    errors: list[str] = []
    service = make_service(
        gateway, repo, on_error=lambda s, message, _missing: errors.append(message)
    )
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)

        gateway.raise_on_pipe_activity = True
        service.request(scope, Priority.INTERACTIVE)
        assert wait_until(lambda: bool(errors))
    finally:
        service.stop()
    assert repo.entry(scope).state is CacheState.ERROR
    assert [row["PIPE_NAME"] for row in repo.raw_rows(scope)] == ["MY_PIPE"]


def test_pipe_activity_history_scope_reaches_the_right_gateway_call(repo) -> None:
    from nivaria.models import pipe_activity_history_scope

    gateway = FakeGateway(
        pipe_activity_history={
            ("MYDB", "PUBLIC", "MY_PIPE"): [
                {
                    "PIPE_CATALOG_NAME": "MYDB",
                    "PIPE_SCHEMA_NAME": "PUBLIC",
                    "PIPE_NAME": "MY_PIPE",
                    "FILE_NAME": "orders_1.csv.gz",
                    "FILE_SIZE": 1024,
                    "ROW_COUNT": 42,
                    "STATUS": "Loaded",
                    "LAST_LOAD_TIME": "2026-01-02T03:04:05+00:00",
                    "TABLE_CATALOG_NAME": "MYDB",
                    "TABLE_SCHEMA_NAME": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                }
            ]
        }
    )
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", "MY_PIPE")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [row["FILE_NAME"] for row in repo.raw_rows(scope)] == ["orders_1.csv.gz"]
    assert [c[0] for c in gateway.calls] == ["pipe_activity_history"]


def test_pipe_history_resolves_its_target_from_cached_pipe_activity(repo) -> None:
    """FetchService._resolve_pipe_target reads the target table straight off
    whatever PIPE_ACTIVITY already has cached, Snowflake's own resolution --
    an opportunistic read of a sibling scope's cache."""
    from nivaria.models import pipe_activity_scope, pipe_history_scope

    activity_scope = pipe_activity_scope("MYDB", "PUBLIC")
    history_scope = pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS")
    gateway = FakeGateway(
        pipe_activity={
            ("MYDB", "PUBLIC"): [
                {
                    "PIPE_CATALOG_NAME": "MYDB",
                    "PIPE_SCHEMA_NAME": "PUBLIC",
                    "PIPE_NAME": "LOAD_ORDERS",
                    "FILE_NAME": "orders_1.csv.gz",
                    "FILE_SIZE": 1024,
                    "ROW_COUNT": 42,
                    "STATUS": "Loaded",
                    "LAST_LOAD_TIME": "2026-01-02T03:04:05+00:00",
                    "TABLE_CATALOG_NAME": "MYDB",
                    "TABLE_SCHEMA_NAME": "PUBLIC",
                    "TABLE_NAME": "ORDERS",
                }
            ]
        },
        pipe_history={
            ("MYDB", "PUBLIC", "ORDERS", "LOAD_ORDERS"): [
                {
                    "FILE_NAME": "orders_9.csv.gz",
                    "FILE_SIZE": 2048,
                    "ROW_COUNT": 100,
                    "STATUS": "Loaded",
                    "LAST_LOAD_TIME": "2026-03-01T08:00:00+00:00",
                    "PIPE_NAME": "LOAD_ORDERS",
                }
            ]
        },
    )
    service = make_service(gateway, repo)
    try:
        service.request(activity_scope)
        assert wait_until(
            lambda: repo.entry(activity_scope).state is CacheState.COMPLETE
        )
        service.request(history_scope)
        assert wait_until(
            lambda: repo.entry(history_scope).state is CacheState.COMPLETE
        )
    finally:
        service.stop()
    assert [row["FILE_NAME"] for row in repo.raw_rows(history_scope)] == [
        "orders_9.csv.gz"
    ]
    assert (
        "pipe_history",
        ("MYDB", "PUBLIC", "ORDERS", "LOAD_ORDERS", 5000),
    ) in gateway.calls


def test_pipe_history_fails_gracefully_with_no_pipe_activity_data_cached(repo) -> None:
    """Nothing to resolve the target table from yet -- e.g. the schema's
    pipe-activity scope has never been fetched, or this pipe genuinely has no
    load history at all. This must fail the one scope, not crash the worker
    thread and strand every later job (see FetchService._execute)."""
    from nivaria.models import pipe_history_scope

    scope = pipe_history_scope("MYDB", "PUBLIC", "LOAD_ORDERS")
    errors: list[str] = []
    service = make_service(
        FakeGateway(),
        repo,
        on_error=lambda s, message, _missing: errors.append(message),
    )
    try:
        service.request(scope)
        assert wait_until(lambda: bool(errors))
    finally:
        service.stop()
    assert repo.entry(scope).state is CacheState.ERROR


def test_task_activity_scope_reaches_the_right_gateway_call(repo) -> None:
    from nivaria.models import task_activity_scope

    gateway = FakeGateway(
        task_activity={
            ("MYDB", "PUBLIC"): [
                {
                    "NAME": "REFRESH_ORDERS",
                    "DATABASE_NAME": "MYDB",
                    "SCHEMA_NAME": "PUBLIC",
                    "QUERY_ID": "01ab-query-id",
                    "SCHEDULED_TIME": "2026-01-02T03:04:05+00:00",
                    "STATE": "SUCCEEDED",
                }
            ]
        }
    )
    scope = task_activity_scope("MYDB", "PUBLIC")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [row["NAME"] for row in repo.raw_rows(scope)] == ["REFRESH_ORDERS"]
    assert [c[0] for c in gateway.calls] == ["task_activity"]


def test_a_failing_task_activity_fetch_keeps_previously_cached_activity(repo) -> None:
    """Same "a failed pass does not sweep" guarantee as pipe_activity's own
    test -- losing the ACCOUNT_USAGE grant or the warehouse mid-browse must
    not blank out activity that was already fetched successfully."""
    from nivaria.models import task_activity_scope

    gateway = FakeGateway(
        task_activity={
            ("MYDB", "PUBLIC"): [
                {
                    "NAME": "REFRESH_ORDERS",
                    "DATABASE_NAME": "MYDB",
                    "SCHEMA_NAME": "PUBLIC",
                    "QUERY_ID": "01ab-query-id",
                    "SCHEDULED_TIME": "2026-01-02T03:04:05+00:00",
                    "STATE": "SUCCEEDED",
                }
            ]
        }
    )
    scope = task_activity_scope("MYDB", "PUBLIC")
    errors: list[str] = []
    service = make_service(
        gateway, repo, on_error=lambda s, message, _missing: errors.append(message)
    )
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)

        gateway.raise_on_task_activity = True
        service.request(scope, Priority.INTERACTIVE)
        assert wait_until(lambda: bool(errors))
    finally:
        service.stop()
    assert repo.entry(scope).state is CacheState.ERROR
    assert [row["NAME"] for row in repo.raw_rows(scope)] == ["REFRESH_ORDERS"]


def test_task_activity_history_scope_reaches_the_right_gateway_call(repo) -> None:
    from nivaria.models import task_activity_history_scope

    gateway = FakeGateway(
        task_activity_history={
            ("MYDB", "PUBLIC", "REFRESH_ORDERS"): [
                {
                    "NAME": "REFRESH_ORDERS",
                    "DATABASE_NAME": "MYDB",
                    "SCHEMA_NAME": "PUBLIC",
                    "QUERY_ID": "01ab-query-id",
                    "SCHEDULED_TIME": "2026-01-02T03:04:05+00:00",
                    "STATE": "SUCCEEDED",
                }
            ]
        }
    )
    scope = task_activity_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [row["NAME"] for row in repo.raw_rows(scope)] == ["REFRESH_ORDERS"]
    assert [c[0] for c in gateway.calls] == ["task_activity_history"]


def test_task_history_scope_reaches_the_right_gateway_call(repo) -> None:
    """Unlike pipe_history, this needs no sibling scope to resolve a target
    from first -- DATABASE_NAME/SCHEMA_NAME/TASK_NAME come straight off the
    scope, so requesting it alone is enough to complete the pass."""
    from nivaria.models import task_history_scope

    gateway = FakeGateway(
        task_history={
            ("MYDB", "PUBLIC", "REFRESH_ORDERS"): [
                {
                    "NAME": "REFRESH_ORDERS",
                    "QUERY_ID": "01ab-query-id",
                    "SCHEDULED_TIME": "2026-03-01T08:00:00+00:00",
                    "STATE": "SUCCEEDED",
                }
            ]
        }
    )
    scope = task_history_scope("MYDB", "PUBLIC", "REFRESH_ORDERS")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert [row["NAME"] for row in repo.raw_rows(scope)] == ["REFRESH_ORDERS"]
    assert [c[0] for c in gateway.calls] == ["task_history"]


# --------------------------------------------------------------- paging guards
#
# The scheduler's continuation chain asks for a page, and stops when the page
# comes back short. Both assumptions underneath that have been observed to
# fail against a real account, so both are tested with a gateway that breaks
# them deliberately.


def test_a_show_that_ignores_limit_is_not_paged_forever(repo) -> None:
    """SHOW PIPES/STAGES return every row whatever LIMIT asks for, verified on
    two unrelated accounts. The continuation used to read the full page as
    "there is more", re-issue with FROM '<last name>', get the identical
    result back, and never terminate -- pinning a worker and a pool connection
    re-querying forever. One statement, one complete pass."""
    from nivaria.models import objects_scope

    pipes = [f"PIPE_{i:04d}" for i in range(25)]
    gateway = FakeGateway(objects={("MYDB", "PUBLIC", ObjectType.PIPE): pipes})
    gateway.paging_noop_types = {ObjectType.PIPE}
    scope = objects_scope(ObjectType.PIPE, "MYDB", "PUBLIC")
    # A page size under the pipe count is what makes the full page look like a
    # partial one -- the real trigger is a schema with more pipes than the
    # page-size setting, which a 400-pipe account reaches by lowering it.
    service = make_service(gateway, repo, page_size=10)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert repo.child_count(scope) == 25
    assert [call[0] for call in gateway.calls].count("show_objects") == 1


def test_a_stuck_cursor_completes_the_pass_rather_than_failing_it(repo) -> None:
    """Deliberately a type the builder still pages -- TASK, whose LIMIT
    support no probed account has had enough tasks to verify. Layer one only
    covers the types already caught misbehaving; this is the guard that has to
    hold for the next one, without anybody having to have found it first.

    Completing rather than failing is the point: a cursor that does not
    advance means the page was the whole result, so the rows are complete and
    current. Failing would throw away a good result to report a paging quirk.
    """
    from nivaria.models import objects_scope

    gateway = FakeGateway(
        objects={("MYDB", "PUBLIC", ObjectType.TASK): ["T1", "T2", "T3"]}
    )
    gateway.paging_noop_types = {ObjectType.TASK}
    scope = objects_scope(ObjectType.TASK, "MYDB", "PUBLIC")
    service = make_service(gateway, repo, page_size=2)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    entry = repo.entry(scope)
    assert entry.error is None
    assert entry.row_count == 3
    # One page, then one repeat that gave the guard something to notice --
    # and then it stops, rather than a third asking for the same tail again.
    assert [call[0] for call in gateway.calls].count("show_objects") == 2


def test_a_result_that_never_ends_stops_at_the_page_budget(repo) -> None:
    """The cursor keeps moving, so the stuck-cursor guard never fires, and the
    result never comes back short. Nothing else bounds the chain, so the page
    budget does -- and it fails the pass rather than finishing one, because
    unlike a stuck cursor this really is an incomplete result."""
    from nivaria.models import databases_scope
    from nivaria.settings import MAX_PAGES_PER_PASS, config

    class NeverEndingGateway(FakeGateway):
        """Every page is full, and every page has a new last name."""

        def show_databases(self, *, after=None, limit=1000, allow_reconnect=True):
            self.calls.append(("show_databases", (after, limit)))
            start = 0 if after is None else int(after.split("_")[1]) + 1
            return [
                {"name": f"DB_{index:06d}", "comment": ""}
                for index in range(start, start + limit)
            ]

    config.set(MAX_PAGES_PER_PASS, 4)
    gateway = NeverEndingGateway()
    scope = databases_scope()
    service = make_service(gateway, repo, page_size=5)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.ERROR)
    finally:
        service.stop()
    assert len(gateway.calls) == 4
    assert "4 pages" in (repo.entry(scope).error or "")


def test_a_failed_page_budget_leaves_the_rows_that_were_already_there(repo) -> None:
    """Same rule as any other failed pass: no sweep, so what was on screen
    stays on screen. A half-fetched scope must not look emptier than the last
    good one."""
    from nivaria.models import databases_scope
    from nivaria.settings import MAX_PAGES_PER_PASS, config

    class NeverEndingGateway(FakeGateway):
        def show_databases(self, *, after=None, limit=1000, allow_reconnect=True):
            self.calls.append(("show_databases", (after, limit)))
            start = 0 if after is None else int(after.split("_")[1]) + 1
            return [
                {"name": f"DB_{index:06d}", "comment": ""}
                for index in range(start, start + limit)
            ]

    scope = databases_scope()
    generation = repo.begin_pass(scope)
    repo.write_page(scope, generation, [{"name": "YESTERDAY", "comment": ""}], None)
    repo.finish_pass(scope, generation)

    config.set(MAX_PAGES_PER_PASS, 2)
    service = make_service(NeverEndingGateway(), repo, page_size=5)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.ERROR)
    finally:
        service.stop()
    assert "YESTERDAY" in [row["name"] for row in repo.raw_rows(scope)]


# ------------------------------------------------------------ the history cap


def _pipe_loads(count: int) -> list[dict[str, object]]:
    return [
        {
            "PIPE_CATALOG_NAME": "MYDB",
            "PIPE_SCHEMA_NAME": "PUBLIC",
            "PIPE_NAME": "MY_PIPE",
            "FILE_NAME": f"orders_{index:05d}.csv.gz",
            "FILE_SIZE": 1024,
            "ROW_COUNT": 1,
            "STATUS": "Loaded",
            "LAST_LOAD_TIME": f"2026-01-02T03:04:{index % 60:02d}+00:00",
            "TABLE_CATALOG_NAME": "MYDB",
            "TABLE_SCHEMA_NAME": "PUBLIC",
            "TABLE_NAME": "ORDERS",
        }
        for index in range(count)
    ]


def test_a_history_at_its_row_cap_is_recorded_as_truncated(repo) -> None:
    """A single busy pipe's history is the one result whose size follows how
    often it *runs* rather than how many objects exist -- 433892 rows over 13
    days on a real account. It is capped, and a capped pass has to say so:
    the table would otherwise just stop, indistinguishable from a complete
    one."""
    from nivaria.models import pipe_activity_history_scope
    from nivaria.settings import HISTORY_MAX_ROWS, config

    config.set(HISTORY_MAX_ROWS, 10)
    gateway = FakeGateway(
        pipe_activity_history={("MYDB", "PUBLIC", "MY_PIPE"): _pipe_loads(50)}
    )
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", "MY_PIPE")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    entry = repo.entry(scope)
    assert entry.truncated
    assert entry.row_count == 10


def test_a_history_inside_its_row_cap_is_not_recorded_as_truncated(repo) -> None:
    """The common case, and the one that decides whether the note above is
    noise: an ordinary pipe's history must not claim to be capped."""
    from nivaria.models import pipe_activity_history_scope
    from nivaria.settings import HISTORY_MAX_ROWS, config

    config.set(HISTORY_MAX_ROWS, 100)
    gateway = FakeGateway(
        pipe_activity_history={("MYDB", "PUBLIC", "MY_PIPE"): _pipe_loads(7)}
    )
    scope = pipe_activity_history_scope("MYDB", "PUBLIC", "MY_PIPE")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert not repo.entry(scope).truncated


def test_the_history_row_cap_reaches_the_statement(repo) -> None:
    """Read at the point of use, like every other setting -- a cap that only
    took effect on restart would be worse than none, since the reason to reach
    for it is a history that is hurting right now."""
    from nivaria.models import task_activity_history_scope
    from nivaria.settings import HISTORY_MAX_ROWS, config

    config.set(HISTORY_MAX_ROWS, 42)
    gateway = FakeGateway()
    scope = task_activity_history_scope("MYDB", "PUBLIC", "MY_TASK")
    service = make_service(gateway, repo)
    try:
        service.request(scope)
        assert wait_until(lambda: repo.entry(scope).state is CacheState.COMPLETE)
    finally:
        service.stop()
    assert gateway.calls == [
        ("task_activity_history", ("MYDB", "PUBLIC", "MY_TASK", 90, 42))
    ]


def test_a_data_preview_is_folded_into_one_json_object_per_row() -> None:
    """Every table has its own columns, so each row is stored whole, keyed by
    column name in SELECT * order. What JSON has no type for keeps its
    meaning: a NUMBER(38,x) its digits, a BINARY Snowflake's hex."""
    import datetime
    import json
    from decimal import Decimal

    from nivaria.models import data_preview_scope

    gateway = FakeGateway(
        data_preview={
            ("MYDB", "PUBLIC", "ORDERS"): [
                {
                    "ID": 1,
                    "TOTAL": Decimal("12345678901234567890.0123456789"),
                    "AT": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
                    "RAW": b"\x01\xab",
                    "NOTE": None,
                },
            ]
        }
    )
    page = fetch_page(gateway, data_preview_scope("MYDB", "PUBLIC", "ORDERS"), None, 1)
    assert [row["_row_number"] for row in page.rows] == [0]
    values = json.loads(page.rows[0]["_values"])
    assert list(values) == ["ID", "TOTAL", "AT", "RAW", "NOTE"]
    assert values == {
        "ID": 1,
        "TOTAL": "12345678901234567890.0123456789",
        "AT": "2026-01-02T03:04:05+00:00",
        "RAW": "01AB",
        "NOTE": None,
    }


def test_a_data_preview_at_its_row_limit_is_truncated() -> None:
    from nivaria.models import data_preview_scope
    from nivaria.settings import DATA_PREVIEW_ROWS, config

    config.set(DATA_PREVIEW_ROWS, 3)
    gateway = FakeGateway(
        data_preview={("MYDB", "PUBLIC", "ORDERS"): [{"ID": i} for i in range(10)]}
    )
    page = fetch_page(gateway, data_preview_scope("MYDB", "PUBLIC", "ORDERS"), None, 1)
    assert len(page.rows) == 3
    assert page.truncated
    assert gateway.calls[0][1][:4] == ("MYDB", "PUBLIC", "ORDERS", 3)


def test_a_data_preview_is_asked_for_with_the_configured_timeout() -> None:
    """A view's preview runs the view's own query, so the gateway is told
    how long it may take before Snowflake is asked to cancel it."""
    from nivaria.models import data_preview_scope
    from nivaria.settings import DATA_PREVIEW_TIMEOUT, config

    config.set(DATA_PREVIEW_TIMEOUT, 7)
    gateway = FakeGateway(data_preview={("MYDB", "PUBLIC", "V"): [{"ID": 1}]})
    fetch_page(gateway, data_preview_scope("MYDB", "PUBLIC", "V"), None, 1)
    assert gateway.calls == [("select_data_preview", ("MYDB", "PUBLIC", "V", 100, 7))]


def test_a_data_preview_inside_its_row_limit_is_the_whole_table() -> None:
    from nivaria.models import data_preview_scope
    from nivaria.settings import DATA_PREVIEW_ROWS, config

    config.set(DATA_PREVIEW_ROWS, 3)
    gateway = FakeGateway(
        data_preview={("MYDB", "PUBLIC", "ORDERS"): [{"ID": 1}, {"ID": 2}]}
    )
    page = fetch_page(gateway, data_preview_scope("MYDB", "PUBLIC", "ORDERS"), None, 1)
    assert len(page.rows) == 2
    assert not page.truncated
