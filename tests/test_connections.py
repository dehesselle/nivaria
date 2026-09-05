from __future__ import annotations

import json

from snowcone.connections import RecentConnections, RecentEntry
from snowcone.gateway.pool import USERNAME_PASSWORD_MFA, ConnectParams


def store(tmp_path) -> RecentConnections:
    return RecentConnections(tmp_path / "recent.json")


def test_a_missing_file_means_no_recents(tmp_path) -> None:
    assert store(tmp_path).load() == []


def test_a_corrupt_file_means_no_recents(tmp_path) -> None:
    """A bad recents file must not stop the app from opening."""
    path = tmp_path / "recent.json"
    path.write_text("{ not json")
    assert RecentConnections(path).load() == []


def test_entries_round_trip(tmp_path) -> None:
    recent = store(tmp_path)
    recent.add(RecentEntry(account="ACME", user="me", role="ANALYST"))
    assert recent.load() == [RecentEntry(account="ACME", user="me", role="ANALYST")]


def test_no_password_is_ever_written(tmp_path) -> None:
    """The whole point of the store: snowcone holds no secret on disk."""
    recent = store(tmp_path)
    params = ConnectParams(
        account="ACME",
        user="me",
        authenticator=USERNAME_PASSWORD_MFA,
        password="s3cret",
    )
    recent.add(RecentEntry.from_params(params))
    raw = (tmp_path / "recent.json").read_text()
    assert "s3cret" not in raw
    # Checked on parsed keys, not the raw text: "password" legitimately occurs
    # inside the authenticator name "username_password_mfa".
    stored = json.loads(raw)
    assert all("password" not in entry for entry in stored)


def test_adding_an_existing_connection_promotes_it(tmp_path) -> None:
    recent = store(tmp_path)
    recent.add(RecentEntry(account="A", user="me"))
    recent.add(RecentEntry(account="B", user="me"))
    recent.add(RecentEntry(account="A", user="me"))
    assert [e.account for e in recent.load()] == ["A", "B"]


def test_a_different_authenticator_is_a_different_entry(tmp_path) -> None:
    """The same account reached two ways is two genuinely different setups."""
    recent = store(tmp_path)
    recent.add(RecentEntry(account="A", user="me"))
    recent.add(RecentEntry(account="A", user="me", authenticator=USERNAME_PASSWORD_MFA))
    assert len(recent.load()) == 2


def test_a_changed_role_updates_rather_than_duplicates(tmp_path) -> None:
    recent = store(tmp_path)
    recent.add(RecentEntry(account="A", user="me", role="READER"))
    recent.add(RecentEntry(account="A", user="me", role="ADMIN"))
    assert [e.role for e in recent.load()] == ["ADMIN"]


def test_the_list_is_capped(tmp_path) -> None:
    recent = store(tmp_path)
    for index in range(15):
        recent.add(RecentEntry(account=f"ACCT{index}", user="me"))
    assert len(recent.load()) == 10
    assert recent.load()[0].account == "ACCT14"


def test_removing_an_entry(tmp_path) -> None:
    recent = store(tmp_path)
    recent.add(RecentEntry(account="A", user="me"))
    recent.add(RecentEntry(account="B", user="me"))
    recent.remove(RecentEntry(account="A", user="me"))
    assert [e.account for e in recent.load()] == ["B"]


def test_malformed_entries_are_skipped_not_fatal(tmp_path) -> None:
    path = tmp_path / "recent.json"
    path.write_text(
        json.dumps([{"account": "A"}, "nonsense", {"account": "B", "user": "me"}])
    )
    assert [e.account for e in RecentConnections(path).load()] == ["B"]


def test_the_label_shows_account_user_and_role() -> None:
    assert (
        RecentEntry(account="ACME", user="me", role="ADMIN").label
        == "ACME / me (ADMIN)"
    )
    assert RecentEntry(account="ACME", user="me").label == "ACME / me"


def test_params_round_trip_without_the_password() -> None:
    entry = RecentEntry(account="A", user="me", role="R", warehouse="W")
    params = entry.to_params()
    assert params.role == "R"
    assert params.warehouse == "W"
    assert params.password is None
    assert RecentEntry.from_params(params) == entry


def test_blank_role_and_warehouse_become_none_in_params() -> None:
    """Empty strings would be sent to the driver as real values."""
    params = RecentEntry(account="A", user="me").to_params()
    assert params.role is None
    assert params.warehouse is None
