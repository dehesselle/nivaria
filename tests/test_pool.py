# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest

from nivaria.snowflake import queries
from nivaria.snowflake.pool import (
    CONNECT_TIMEOUT_SECONDS,
    EXTERNAL_BROWSER,
    LOGIN_TIMED_OUT,
    OAUTH,
    USERNAME_PASSWORD_MFA,
    AccountNames,
    ConnectionError_,
    ConnectionPool,
    ConnectParams,
)
from tests.fakes import FakeConnector, LoginTimeoutError, make_pool


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


def test_a_browser_login_that_times_out_while_growing_fails_in_plain_words() -> None:
    logins: list[int] = []
    inner = FakeConnector()

    def connector(**kwargs: Any) -> Any:
        logins.append(1)
        if len(logins) > 1:
            raise LoginTimeoutError("Please check the redirect URI and try again.")
        return inner(**kwargs)

    pool = ConnectionPool(ConnectParams(account="a", user="u"), 1, connector=connector)
    pool.connect_all()
    with pytest.raises(ConnectionError_) as excinfo:
        pool.grow(1)
    assert str(excinfo.value) == LOGIN_TIMED_OUT


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


def _refuse_logins(connector: FakeConnector) -> None:
    connector.fail_after = len(connector.calls)


def _run_in_thread(call) -> tuple[threading.Thread, list[Any]]:
    """Run `call` where a pool that waits forever cannot hang the test run."""
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(call())
        except Exception as error:  # noqa: BLE001 -- the test reads it
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _acquire_once(pool: ConnectionPool, **kwargs: Any) -> Any:
    with pool.acquire(**kwargs) as connection:
        return connection


def test_a_failed_login_gives_up_the_slot_of_a_connection_that_failed_in_use() -> None:
    """`size` counts what exists. Left alone it reads as full, and the next
    checkout waits on a connection nobody will ever hand back."""
    pool, connector = make_pool(size=2)
    pool.connect_all()
    _refuse_logins(connector)
    with pytest.raises(ConnectionError_), pool.acquire():
        raise ValueError("session expired")
    assert pool.size == 1
    assert len(pool._all) == 1
    assert pool.in_use == 0


def test_a_failed_login_gives_up_the_slot_of_a_connection_found_closed() -> None:
    pool, connector = make_pool(size=2)
    pool.connect_all()
    for connection in pool._all:
        connection.closed = True
    _refuse_logins(connector)
    with pytest.raises(ConnectionError_), pool.acquire():
        pass
    assert pool.size == 1
    assert len(pool._all) == 1


def test_a_failed_login_during_a_session_change_gives_up_the_slot() -> None:
    pool, connector = make_pool(size=2)
    pool.connect_all()
    for connection in pool._all:
        connection.closed = True
    _refuse_logins(connector)
    with pytest.raises(ConnectionError_):
        pool.run_on_all('USE WAREHOUSE "WH"')
    assert pool.size == 1
    assert len(pool._all) == 1


def test_a_checkout_after_a_failed_login_tries_again_instead_of_waiting() -> None:
    """The login that failed may work now: the network is back, or the user
    answers this prompt."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    _refuse_logins(connector)
    with pytest.raises(ConnectionError_), pool.acquire():
        raise ValueError("session expired")

    thread, outcome = _run_in_thread(lambda: _acquire_once(pool))
    thread.join(timeout=5)
    assert isinstance(outcome[0], ConnectionError_)

    connector.fail_after = None
    thread, outcome = _run_in_thread(lambda: _acquire_once(pool))
    thread.join(timeout=5)
    assert not outcome[0].is_closed()
    assert pool.size == 1


def test_a_worker_waiting_on_the_last_connection_is_told_when_it_is_gone() -> None:
    """A background job may wait while something is merely busy, but not on a
    connection that was just given up: it would wait forever."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    started = threading.Event()

    def wait() -> Any:
        started.set()
        return _acquire_once(pool, allow_reconnect=False)

    with pytest.raises(ConnectionError_), pool.acquire():
        thread, outcome = _run_in_thread(wait)
        assert started.wait(timeout=2)
        time.sleep(0.05)
        assert not outcome
        _refuse_logins(connector)
        raise ValueError("session expired")
    thread.join(timeout=5)
    assert isinstance(outcome[0], ConnectionError_)


def test_an_interactive_worker_waiting_on_a_given_up_connection_logs_in_itself() -> (
    None
):
    inner = FakeConnector()
    logins: list[int] = []

    def connector(**kwargs: Any) -> Any:
        logins.append(1)
        if len(logins) == 2:
            raise RuntimeError("login timed out")
        return inner(**kwargs)

    pool = ConnectionPool(ConnectParams(account="a", user="u"), 1, connector=connector)
    pool.connect_all()
    started = threading.Event()

    def wait() -> Any:
        started.set()
        return _acquire_once(pool)

    with pytest.raises(ConnectionError_), pool.acquire():
        thread, outcome = _run_in_thread(wait)
        assert started.wait(timeout=2)
        time.sleep(0.05)
        assert not outcome
        raise ValueError("session expired")
    thread.join(timeout=5)
    assert not outcome[0].is_closed()
    assert pool.size == 1
    assert len(logins) == 3


def test_replacement_logins_run_one_at_a_time() -> None:
    """Every session dies together when a laptop sleeps, and each login can be
    a browser window or a push notification. A successful first one caches the
    token, so the ones after it do not prompt."""
    inner = FakeConnector()
    lock = threading.Lock()
    running = [0]
    most = [0]

    def connector(**kwargs: Any) -> Any:
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return inner(**kwargs)

    pool = ConnectionPool(ConnectParams(account="a", user="u"), 3, connector=connector)
    pool.connect_all()
    for connection in pool._all:
        connection.closed = True
    most[0] = 0
    runs = [_run_in_thread(lambda: _acquire_once(pool)) for _ in range(3)]
    for thread, _ in runs:
        thread.join(timeout=5)
    assert most[0] == 1
    assert all(not outcome[0].is_closed() for _, outcome in runs)


def test_a_failed_login_fails_those_waiting_on_it_without_a_prompt_each() -> None:
    """Three windows in a row, each waiting out its timeout, for what one
    unanswered prompt has just shown nobody is there to answer."""
    inner = FakeConnector()
    answer = threading.Event()
    refuse = threading.Event()
    logins: list[int] = []

    def connector(**kwargs: Any) -> Any:
        if not refuse.is_set():
            return inner(**kwargs)
        logins.append(1)
        assert answer.wait(timeout=5)
        raise RuntimeError("login timed out")

    pool = ConnectionPool(ConnectParams(account="a", user="u"), 3, connector=connector)
    pool.connect_all()
    for connection in pool._all:
        connection.closed = True
    refuse.set()

    runs = [_run_in_thread(lambda: _acquire_once(pool)) for _ in range(3)]
    deadline = time.monotonic() + 2
    while not logins and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.1)
    answer.set()
    for thread, _ in runs:
        thread.join(timeout=5)

    assert len(logins) == 1
    for _, outcome in runs:
        assert isinstance(outcome[0], ConnectionError_)
        assert "login timed out" in str(outcome[0])
    assert pool.size == 0


def test_a_login_asked_for_after_one_failed_prompts_again() -> None:
    """Sharing a failure is only for those already waiting on that login. The
    next action is a new request, and may well be answered this time."""
    pool, connector = make_pool(size=1)
    pool.connect_all()
    _refuse_logins(connector)
    with pytest.raises(ConnectionError_), pool.acquire():
        raise ValueError("session expired")
    connector.fail_after = None
    with pool.acquire() as connection:
        assert not connection.is_closed()


_DRIVER_TIMEOUT = (
    "251016 (08001): Failed to connect to DB: acme.snowflakecomputing.com:443, "
    "Unable to receive the OAuth message within a given timeout. Please check "
    "the redirect URI and try again."
)


def _pool_with_a_failing_login(error: Exception) -> ConnectionPool:
    inner = FakeConnector()
    logins: list[int] = []

    def connector(**kwargs: Any) -> Any:
        logins.append(1)
        if len(logins) > 1:
            raise error
        return inner(**kwargs)

    pool = ConnectionPool(ConnectParams(account="a", user="u"), 1, connector=connector)
    pool.connect_all()
    return pool


def test_a_prompt_nobody_answered_fails_in_plain_words(caplog) -> None:
    """The driver's text points at a redirect URI that is fine. It goes to the
    log for anyone who needs it."""
    pool = _pool_with_a_failing_login(LoginTimeoutError(_DRIVER_TIMEOUT))
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(ConnectionError_) as excinfo,
        pool.acquire(),
    ):
        raise ValueError("session expired")
    assert str(excinfo.value) == LOGIN_TIMED_OUT
    assert "redirect URI" in caplog.text


def test_those_waiting_on_an_unanswered_prompt_get_the_same_plain_words() -> None:
    inner = FakeConnector()
    answer = threading.Event()
    refuse = threading.Event()
    logins: list[int] = []

    def connector(**kwargs: Any) -> Any:
        if not refuse.is_set():
            return inner(**kwargs)
        logins.append(1)
        assert answer.wait(timeout=5)
        raise LoginTimeoutError(_DRIVER_TIMEOUT)

    pool = ConnectionPool(ConnectParams(account="a", user="u"), 2, connector=connector)
    pool.connect_all()
    for connection in pool._all:
        connection.closed = True
    refuse.set()
    runs = [_run_in_thread(lambda: _acquire_once(pool)) for _ in range(2)]
    deadline = time.monotonic() + 2
    while not logins and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.1)
    answer.set()
    for thread, _ in runs:
        thread.join(timeout=5)
    assert [str(outcome[0]) for _, outcome in runs] == [LOGIN_TIMED_OUT] * 2


def test_any_other_failed_login_keeps_what_the_driver_said() -> None:
    """A network that is down or an account that is locked is worth reading, and
    a browser login timing out would be the wrong thing to report."""
    pool = _pool_with_a_failing_login(RuntimeError("could not reach the host"))
    with (
        pytest.raises(ConnectionError_, match="could not reach the host"),
        pool.acquire(),
    ):
        raise ValueError("session expired")


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
    with caplog.at_level(logging.DEBUG, logger="nivaria.snowflake.sql_log"):
        pool.set_role("SYSADMIN")
    records = [
        record
        for record in caplog.records
        if record.name == "nivaria.snowflake.sql_log"
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
