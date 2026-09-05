from __future__ import annotations

import threading
import time

import pytest

from snowcone.gateway.pool import (
    EXTERNAL_BROWSER,
    USERNAME_PASSWORD_MFA,
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


def test_sessions_are_kept_alive_and_tokens_cached() -> None:
    """These three flags are the whole reason 'prompt once at startup' holds:
    without them the session expires mid-browse and re-prompts."""
    kwargs = ConnectParams(account="a", user="u").to_connect_kwargs()
    assert kwargs["client_session_keep_alive"] is True
    assert kwargs["client_store_temporary_credential"] is True
    assert kwargs["client_request_mfa_token"] is True


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


def test_a_failure_to_connect_is_reported_as_a_snowcone_error() -> None:
    connector = FakeConnector(fail_after=1)
    pool = ConnectionPool(ConnectParams(account="a", user="u"), 3, connector=connector)
    with pytest.raises(ConnectionError_):
        pool.connect_all()


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


def test_a_pool_must_have_at_least_one_connection() -> None:
    with pytest.raises(ValueError):
        ConnectionPool(ConnectParams(account="a", user="u"), 0)


def test_acquiring_from_a_closed_pool_is_an_error() -> None:
    pool, _ = make_pool(size=1)
    pool.connect_all()
    pool.close()
    with pytest.raises(ConnectionError_), pool.acquire():
        pass
