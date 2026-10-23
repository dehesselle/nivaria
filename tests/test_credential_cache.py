"""DpapiFileKeyring is Windows-only -- see credential_cache.py for why -- so
every test here is skipped on any other platform rather than faked, since
there is nothing to fake: DPAPI is a real OS facility, not a network call or
the Snowflake driver, so exercising it for real costs nothing tests are
supposed to avoid."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="DpapiFileKeyring only exists on Windows"
)

if sys.platform == "win32":
    import keyring
    import keyring.errors

    from nivaria.gateway.credential_cache import DpapiFileKeyring, install


def test_a_stored_secret_round_trips(tmp_path) -> None:
    cache = DpapiFileKeyring(tmp_path / "credential_cache.json")
    cache.set_password("snowflake", "alice", "s3cret-token")
    assert cache.get_password("snowflake", "alice") == "s3cret-token"


def test_a_missing_entry_returns_none(tmp_path) -> None:
    cache = DpapiFileKeyring(tmp_path / "credential_cache.json")
    assert cache.get_password("snowflake", "nobody") is None


def test_the_cache_file_never_holds_the_secret_as_plaintext(tmp_path) -> None:
    """The whole point of DPAPI over a bare file: a copy of the file alone
    (without the Windows profile that wrote it) must not reveal the token."""
    path = tmp_path / "credential_cache.json"
    cache = DpapiFileKeyring(path)
    cache.set_password("snowflake", "alice", "s3cret-token")
    assert "s3cret-token" not in path.read_text(encoding="utf-8")


def test_a_long_secret_that_would_overflow_credential_manager_round_trips(
    tmp_path,
) -> None:
    """The reason this backend exists at all: an OAuth JWT routinely exceeds
    Windows Credential Manager's 2560-byte CRED_TYPE_GENERIC cap."""
    cache = DpapiFileKeyring(tmp_path / "credential_cache.json")
    huge_token = "x" * 8000
    cache.set_password("snowflake", "alice", huge_token)
    assert cache.get_password("snowflake", "alice") == huge_token


def test_two_usernames_under_the_same_service_do_not_collide(tmp_path) -> None:
    cache = DpapiFileKeyring(tmp_path / "credential_cache.json")
    cache.set_password("snowflake", "alice", "alice-token")
    cache.set_password("snowflake", "bob", "bob-token")
    assert cache.get_password("snowflake", "alice") == "alice-token"
    assert cache.get_password("snowflake", "bob") == "bob-token"


def test_deleting_an_entry_removes_it(tmp_path) -> None:
    cache = DpapiFileKeyring(tmp_path / "credential_cache.json")
    cache.set_password("snowflake", "alice", "s3cret-token")
    cache.delete_password("snowflake", "alice")
    assert cache.get_password("snowflake", "alice") is None


def test_deleting_a_missing_entry_raises_the_keyring_contract_error(tmp_path) -> None:
    cache = DpapiFileKeyring(tmp_path / "credential_cache.json")
    with pytest.raises(keyring.errors.PasswordDeleteError):
        cache.delete_password("snowflake", "nobody")


def test_a_missing_cache_file_means_no_entries_not_a_crash(tmp_path) -> None:
    """Every store in nivaria degrades to empty rather than failing --
    consistent with ProfileStore and the sqlite cache."""
    cache = DpapiFileKeyring(tmp_path / "does-not-exist.json")
    assert cache.get_password("snowflake", "alice") is None


def test_install_makes_dpapi_the_active_keyring_backend() -> None:
    original = keyring.get_keyring()
    try:
        install()
        assert isinstance(keyring.get_keyring(), DpapiFileKeyring)
    finally:
        keyring.set_keyring(original)


def test_install_is_idempotent() -> None:
    """A second call must not discard whatever the first one already set up
    -- it runs before every real connect(), not just the first."""
    original = keyring.get_keyring()
    try:
        install()
        first = keyring.get_keyring()
        install()
        assert keyring.get_keyring() is first
    finally:
        keyring.set_keyring(original)
