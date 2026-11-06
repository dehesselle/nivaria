# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest

from nivaria.gateway import queries
from nivaria.gateway.pool import (
    CONNECT_TIMEOUT_SECONDS,
    EXTERNAL_BROWSER,
    OAUTH,
    USERNAME_PASSWORD_MFA,
    AccountNames,
    ConnectionError_,
    ConnectionPool,
    ConnectParams,
)
from tests.fakes import FakeConnector, make_pool


def test_the_password_never_appears_in_a_repr() -> None:
    """A traceback or a `textual console` log must not be able to leak it."""
    params = ConnectParams(
        account="acct", user="u", authenticator=USERNAME_PASSWORD_MFA, password="s3cret"
    )
    assert "s3cret" not in repr(params)


def test_params_compare_equal_regardless_of_password() -> None:
    """A params object carrying a typed password still has to match its
    password-less twin recalled from the recent-connections list."""
    with_pw = ConnectParams(account="a", user="u", password="p")
    without = ConnectParams(account="a", user="u")
    assert with_pw == without


def test_external_browser_never_sends_a_password() -> None:
    """SSO takes credentials from the IdP; forwarding a stale typed password
    would be both useless and a needless place for it to travel."""
    params = ConnectParams(
        account="a", user="u", authenticator=EXTERNAL_BROWSER, password="p"
    )
    assert "password" not in params.to_connect_kwargs()


def test_password_mfa_sends_the_password() -> None:
    params = ConnectParams(
        account="a", user="u", authenticator=USERNAME_PASSWORD_MFA, password="p"
    )
    assert params.to_connect_kwargs()["password"] == "p"


def test_oauth_never_sends_a_password() -> None:
    """OAuth obtains its own credential through a browser popup, same as
    externalbrowser; forwarding a stale typed password would be useless."""
    params = ConnectParams(account="a", user="u", authenticator=OAUTH, password="p")
    assert "password" not in params.to_connect_kwargs()


def test_oauth_asks_the_driver_for_a_cached_refresh_token() -> None:
    """Without this, the authorization-code flow's access token expires and
    every subsequent connection -- in this pool or a later session -- opens a
    fresh browser popup instead of refreshing silently in the background."""
    params = ConnectParams(account="a", user="u", authenticator=OAUTH)
    assert params.to_connect_kwargs()["oauth_enable_refresh_tokens"] is True


def test_non_oauth_authenticators_never_request_refresh_tokens() -> None:
    for authenticator in (EXTERNAL_BROWSER, USERNAME_PASSWORD_MFA):
        params = ConnectParams(account="a", user="u", authenticator=authenticator)
        assert "oauth_enable_refresh_tokens" not in params.to_connect_kwargs()


def test_sessions_are_kept_alive_and_tokens_cached() -> None:
    """These three flags are the whole reason 'prompt once at startup' holds:
    without them the session expires mid-browse and re-prompts."""
    kwargs = ConnectParams(account="a", user="u").to_connect_kwargs()
    assert kwargs["client_session_keep_alive"] is True
    assert kwargs["client_store_temporary_credential"] is True
    assert kwargs["client_request_mfa_token"] is True


def test_a_denied_or_abandoned_approval_is_bounded_by_the_connect_timeout() -> None:
    """Otherwise a denied browser/MFA prompt leaves the connecting dialog
    stuck until the driver's own undocumented default timeout gives up."""
    kwargs = ConnectParams(account="a", user="u").to_connect_kwargs()
    assert kwargs["login_timeout"] == CONNECT_TIMEOUT_SECONDS
    assert kwargs["external_browser_timeout"] == CONNECT_TIMEOUT_SECONDS


def test_connections_identify_themselves_as_nivaria_to_snowflake() -> None:
    """Admins attribute sessions by this, since nivaria deliberately sets no
    QUERY_TAG that would override theirs."""
    kwargs = ConnectParams(account="a", user="u").to_connect_kwargs()
    assert kwargs["application"] == "nivaria"
    assert "session_parameters" not in kwargs


def test_empty_role_and_warehouse_are_omitted_rather_than_sent_blank() -> None:
    kwargs = ConnectParams(
        account="a", user="u", role="", warehouse=""
    ).to_connect_kwargs()
    assert "role" not in kwargs
    assert "warehouse" not in kwargs


def test_connect_all_opens_exactly_the_requested_number() -> None:
    pool, connector = make_pool(size=3)
    pool.connect_all()
    assert len(connector.calls) == 3


def test_connect_all_reports_progress_one_at_a_time() -> None:
    """Connections are opened sequentially so N MFA prompts do not arrive at
    once; the progress callback is what lets the connect screen say so."""
    pool, _ = make_pool(size=3)
    seen: list[tuple[int, int]] = []
    pool.connect_all(progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_connect_all_adopts_the_role_and_warehouse_snowflake_actually_assumed() -> None:
    """A session always gets some role -- and often a default warehouse -- even
    when neither was requested; the footer should show that, not "none"."""
    connector = FakeConnector(resolved_role="PUBLIC", resolved_warehouse="COMPUTE_WH")
    pool = ConnectionPool(ConnectParams(account="a", user="u"), 2, connector=connector)
    pool.connect_all()
    assert pool.params.role == "PUBLIC"
    assert pool.params.warehouse == "COMPUTE_WH"


def test_connect_all_keeps_no_warehouse_as_a_genuine_answer() -> None:
    """Unlike role, a session can genuinely have no active warehouse -- the
    adoption must not invent one where the driver reports none."""
    connector = FakeConnector(resolved_role="PUBLIC", resolved_warehouse=None)
    pool = ConnectionPool(ConnectParams(account="a", user="u"), 1, connector=connector)
    pool.connect_all()
    assert pool.params.warehouse is None


def test_connect_all_never_overrides_an_explicit_role_or_warehouse() -> None:
    connector = FakeConnector(resolved_role="PUBLIC", resolved_warehouse="COMPUTE_WH")
    pool = ConnectionPool(
        ConnectParams(account="a", user="u", role="SYSADMIN", warehouse="ETL_WH"),
        1,
        connector=connector,
    )
    pool.connect_all()
    assert pool.params.role == "SYSADMIN"
    assert pool.params.warehouse == "ETL_WH"


def test_connect_all_reads_the_organization_and_account_name() -> None:
    """For Snowsight links, whatever form the identifier was typed in."""
    pool, _ = make_pool(size=2)
    pool.connect_all()
    assert pool.account_names == AccountNames("MYORG", "MYACCT")


def test_the_account_names_are_asked_once_on_one_connection() -> None:
    """The account cannot change within a pool, so neither more connections
    nor growing the pool asks again."""
    pool, _ = make_pool(size=2)
    pool.connect_all()
    pool.grow(1)
    statement = queries.current_account_names()
    asked = [c.executed.count(statement) for c in pool._all]
    assert asked == [1, 0, 0]


def test_a_failed_account_name_query_still_connects() -> None:
    """The names only feed a link; a working session is worth more."""
    connector = FakeConnector(account_names=None)
    pool = ConnectionPool(
        ConnectParams(account="acct", user="user"), 1, connector=connector
    )
    pool.connect_all()
    assert pool.account_names is None
    with pool.acquire() as connection:
        assert connection is pool._all[0]


def test_a_privatelink_identifier_is_recognised() -> None:
    assert ConnectParams(account="MYORG-MYACCT.PRIVATELINK", user="u").uses_privatelink
    assert not ConnectParams(account="MYORG-MYACCT", user="u").uses_privatelink


def test_a_failure_to_connect_is_reported_as_a_nivaria_error() -> None:
    connector = FakeConnector(fail_after=1)
    pool = ConnectionPool(ConnectParams(account="a", user="u"), 3, connector=connector)
    with pytest.raises(ConnectionError_):
        pool.connect_all()


def test_grow_opens_exactly_the_additional_connections() -> None:
    pool, connector = make_pool(size=1)
    pool.connect_all()
    pool.grow(2)
    assert len(connector.calls) == 3
    assert pool.size == 3


def test_grow_reports_progress_against_the_new_total() -> None:
    """The connect screen only asks about the connections beyond the first,
    so progress has to count from where that first one left off."""
    pool, _ = make_pool(size=1)
    pool.connect_all()
    seen: list[tuple[int, int]] = []
    pool.grow(2, progress=lambda done, total: seen.append((done, total)))
    assert seen == [(2, 3), (3, 3)]


def test_a_failure_to_grow_is_reported_as_a_nivaria_error() -> None:
    connector = FakeConnector(fail_after=1)
    pool = ConnectionPool(ConnectParams(account="a", user="u"), 1, connector=connector)
    pool.connect_all()
    with pytest.raises(ConnectionError_):
        pool.grow(2)


def test_grow_requires_at_least_one_additional_connection() -> None:
    pool, _ = make_pool(size=1)
    pool.connect_all()
    with pytest.raises(ValueError):
        pool.grow(0)


def test_acquire_hands_out_and_returns_a_connection() -> None:
    pool, _ = make_pool(size=1)
    pool.connect_all()
    assert pool.in_use == 0
    with pool.acquire():
        assert pool.in_use == 1
    assert pool.in_use == 0


def test_in_use_is_restored_even_when_the_block_raises() -> None:
    """A worker whose query fails must not leak a connection out of the pool,
    or the gauge creeps up to (N/N) and everything stalls forever."""
    pool, _ = make_pool(size=1)
    pool.connect_all()
    with pytest.raises(ValueError), pool.acquire():
        raise ValueError("boom")
    assert pool.in_use == 0


def test_usage_changes_are_reported_for_the_footer_gauge() -> None:
    seen: list[tuple[int, int]] = []
    pool, _ = make_pool(
        size=2, on_usage_change=lambda used, size: seen.append((used, size))
    )
    pool.connect_all()
    with pool.acquire():
        pass
    assert (1, 2) in seen
    assert seen[-1] == (0, 2)


def test_a_worker_blocks_while_every_connection_is_busy() -> None:
    """This is the 'stalled worker' state: it needs no special handling beyond
    blocking here, but it must actually block rather than hand out a second
    borrow of the same connection."""
    pool, _ = make_pool(size=1)
    pool.connect_all()
    released = threading.Event()
    acquired_second = threading.Event()

    def second() -> None:
        with pool.acquire():
            acquired_second.set()

    with pool.acquire():
        thread = threading.Thread(target=second, daemon=True)
        thread.start()
        time.sleep(0.05)
        assert not acquired_second.is_set()
        released.set()
    thread.join(timeout=2)
    assert acquired_second.is_set()


def test_a_closed_connection_is_replaced_on_acquire() -> None:
    """Snowflake drops idle sessions; handing a dead one to a worker would
    surface as a confusing query error instead of a transparent reconnect."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    with pool.acquire() as connection:
        connection.closed = True
    with pool.acquire() as replacement:
        assert not replacement.is_closed()
    assert len(connector.calls) == 2


def test_the_pool_actually_heals_after_replacing_a_closed_connection() -> None:
    """Regression: acquire() used to recycle the *original* dead connection
    back into the free queue instead of the replacement, so the pool never
    healed -- every later acquire reconnected fresh again and leaked the
    one-shot connection it handed out, forever."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    with pool.acquire() as connection:
        connection.closed = True
    with pool.acquire() as replacement:
        assert not replacement.is_closed()
    # A third acquire must reuse the now-healthy replacement, not reconnect
    # yet again.
    with pool.acquire() as third:
        assert third is replacement
    assert len(connector.calls) == 2


def test_background_priority_skips_a_dead_connection_for_a_live_one() -> None:
    """The staleness sweep must never open a new session unattended, but it
    also should not fail just because the one connection it happened to draw
    is dead while others are perfectly fine."""
    pool, connector = make_pool(size=2)
    pool.connect_all()
    with pool.acquire() as first:
        first.closed = True
    with pool.acquire(allow_reconnect=False) as connection:
        assert not connection.is_closed()
    # No reconnect attempted: still exactly the two connections opened by
    # connect_all, and the dead one's slot is gone.
    assert len(connector.calls) == 2
    assert pool.size == 1


def test_background_priority_fails_fast_when_nothing_idle_is_alive() -> None:
    """Per the design: better to surface 'the pool is empty' immediately than
    block or silently open a new, possibly-interactive session unattended."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    with pool.acquire() as connection:
        connection.closed = True
    with pytest.raises(ConnectionError_), pool.acquire(allow_reconnect=False):
        pass
    assert len(connector.calls) == 1
    assert pool.size == 0


def test_an_interactive_checkout_repairs_a_slot_a_background_job_dropped() -> None:
    """The gap a background drop leaves behind is only ever repaired by an
    interactive/visible caller, on demand -- the background sweep itself must
    never grow the pool back by reconnecting."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    with pool.acquire() as connection:
        connection.closed = True
    with pytest.raises(ConnectionError_), pool.acquire(allow_reconnect=False):
        pass
    assert pool.size == 0
    with pool.acquire() as repaired:
        assert not repaired.is_closed()
    assert pool.size == 1
    assert len(connector.calls) == 2


def test_background_priority_shrinks_the_pool_on_a_mid_use_failure() -> None:
    """A connection that fails once actually queried -- the case a checkout
    time is_closed() check cannot see -- must not be replaced unattended
    either; it is dropped and the gauge shrinks instead."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    with pytest.raises(ValueError), pool.acquire(allow_reconnect=False):
        raise ValueError("boom")
    assert len(connector.calls) == 1
    assert pool.size == 0


def test_interactive_priority_still_reconnects_transparently() -> None:
    """The default behaviour -- someone actively at the keyboard -- is
    unaffected by the background-priority carve-out."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    with pool.acquire() as connection:
        connection.closed = True
    with pool.acquire(allow_reconnect=True) as replacement:
        assert not replacement.is_closed()
    assert len(connector.calls) == 2
    assert pool.size == 1


def test_a_pool_must_have_at_least_one_connection() -> None:
    with pytest.raises(ValueError):
        ConnectionPool(ConnectParams(account="a", user="u"), 0)


def test_acquiring_from_a_closed_pool_is_an_error() -> None:
    pool, _ = make_pool(size=1)
    pool.connect_all()
    pool.close()
    with pytest.raises(ConnectionError_), pool.acquire():
        pass


def test_close_leaves_a_still_checked_out_connection_alone() -> None:
    """Regression: closing a connection out from under a worker thread's
    in-flight cursor.execute() races the driver's own session teardown on
    the same underlying HTTP session and was observed to hang the whole app
    on quit (and on a fast reconnect, which follows the identical
    stop-then-close sequence). Workers cannot be interrupted, so a
    still-checked-out connection is abandoned instead of raced; only idle
    ones are actually closed."""
    pool, _ = make_pool(size=2)
    pool.connect_all()
    held_ready = threading.Event()
    release = threading.Event()
    held: list[Any] = []

    def hold() -> None:
        with pool.acquire() as connection:
            held.append(connection)
            held_ready.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held_ready.wait(timeout=2)
    idle_connection = next(c for c in pool._all if c is not held[0])

    pool.close()

    assert not held[0].is_closed()
    assert idle_connection.is_closed()
    release.set()
    thread.join(timeout=2)


# --------------------------------------------------------- session-wide changes


def test_set_role_runs_use_role_on_every_connection() -> None:
    """SHOW results are filtered by role, so every pooled connection has to
    agree or which role served a query would depend on which one was free."""
    pool, _ = make_pool(size=2)
    pool.connect_all()
    pool.set_role("SYSADMIN")
    assert all(c.executed[-1] == 'USE ROLE "SYSADMIN"' for c in pool._all)
    assert pool.params.role == "SYSADMIN"


def test_set_warehouse_runs_use_warehouse_on_every_connection() -> None:
    pool, _ = make_pool(size=2)
    pool.connect_all()
    pool.set_warehouse("COMPUTE_WH")
    assert all(c.executed[-1] == 'USE WAREHOUSE "COMPUTE_WH"' for c in pool._all)
    assert pool.params.warehouse == "COMPUTE_WH"


def test_run_on_all_logs_the_statement_at_debug_under_the_sql_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool, _ = make_pool(size=2)
    pool.connect_all()
    with caplog.at_level(logging.DEBUG, logger="nivaria.gateway.sql_log"):
        pool.set_role("SYSADMIN")
    records = [
        record for record in caplog.records if record.name == "nivaria.gateway.sql_log"
    ]
    assert len(records) == 2  # one per pooled connection
    assert all('USE ROLE "SYSADMIN"' in record.message for record in records)


def test_a_dead_connection_is_replaced_during_a_session_change() -> None:
    """Draining the pool for a role/warehouse switch has to heal a connection
    Snowflake already dropped, the same as an ordinary acquire() would."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    pool._all[0].closed = True
    pool.set_role("SYSADMIN")
    assert len(connector.calls) == 2
    assert pool.params.role == "SYSADMIN"


def test_run_on_all_waits_for_an_in_flight_query_to_finish() -> None:
    """A connection mid-query cannot have a second cursor opened on it, so the
    switch has to wait for it to come back rather than seize it."""
    pool, _ = make_pool(size=1)
    pool.connect_all()
    finished = threading.Event()

    def switch() -> None:
        pool.set_role("SYSADMIN")
        finished.set()

    with pool.acquire():
        thread = threading.Thread(target=switch, daemon=True)
        thread.start()
        time.sleep(0.05)
        assert not finished.is_set()
    thread.join(timeout=2)
    assert finished.is_set()
    assert pool.params.role == "SYSADMIN"


def test_acquire_waits_behind_an_in_progress_session_change() -> None:
    """The other half of the same guarantee: a worker must not check out a
    connection while a switch is mid-way through rewriting the pool."""
    pool, _ = make_pool(size=1)
    pool.connect_all()
    acquired = threading.Event()
    pool._maintenance.acquire()
    try:

        def go() -> None:
            with pool.acquire():
                acquired.set()

        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        time.sleep(0.05)
        assert not acquired.is_set()
    finally:
        pool._maintenance.release()
    thread.join(timeout=2)
    assert acquired.is_set()


def test_run_on_all_times_out_rather_than_hanging_forever() -> None:
    """A connection that is merely busy elsewhere must not be able to wedge a
    session change forever -- it surfaces as an error instead."""
    pool, _ = make_pool(size=1)
    pool.connect_all()
    with pool.acquire(), pytest.raises(ConnectionError_):
        pool.run_on_all('USE ROLE "X"', timeout=0.05)
