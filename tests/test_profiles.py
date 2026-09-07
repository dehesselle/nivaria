from __future__ import annotations

import json

from snowcone.gateway.pool import USERNAME_PASSWORD_MFA, ConnectParams
from snowcone.profiles import Profile, ProfileStore


def store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "recent.json")


def test_a_missing_file_means_no_recents(tmp_path) -> None:
    assert store(tmp_path).load() == []


def test_a_corrupt_file_means_no_recents(tmp_path) -> None:
    """A bad recents file must not stop the app from opening."""
    path = tmp_path / "recent.json"
    path.write_text("{ not json")
    assert ProfileStore(path).load() == []


def test_entries_round_trip(tmp_path) -> None:
    profiles = store(tmp_path)
    profiles.add(Profile(account="ACME", user="me", role="ANALYST"))
    assert profiles.load() == [Profile(account="ACME", user="me", role="ANALYST")]


def test_no_password_is_ever_written(tmp_path) -> None:
    """The whole point of the store: snowcone holds no secret on disk."""
    profiles = store(tmp_path)
    params = ConnectParams(
        account="ACME",
        user="me",
        authenticator=USERNAME_PASSWORD_MFA,
        password="s3cret",
    )
    profiles.add(Profile.from_params(params))
    raw = (tmp_path / "recent.json").read_text()
    assert "s3cret" not in raw
    # Checked on parsed keys, not the raw text: "password" legitimately occurs
    # inside the authenticator name "username_password_mfa".
    stored = json.loads(raw)
    assert all("password" not in entry for entry in stored)


def test_adding_an_existing_connection_promotes_it(tmp_path) -> None:
    profiles = store(tmp_path)
    profiles.add(Profile(account="A", user="me"))
    profiles.add(Profile(account="B", user="me"))
    profiles.add(Profile(account="A", user="me"))
    assert [p.account for p in profiles.load()] == ["A", "B"]


def test_a_different_authenticator_is_a_different_entry(tmp_path) -> None:
    """The same account reached two ways is two genuinely different setups."""
    profiles = store(tmp_path)
    profiles.add(Profile(account="A", user="me"))
    profiles.add(Profile(account="A", user="me", authenticator=USERNAME_PASSWORD_MFA))
    assert len(profiles.load()) == 2


def test_a_changed_role_updates_rather_than_duplicates(tmp_path) -> None:
    profiles = store(tmp_path)
    profiles.add(Profile(account="A", user="me", role="READER"))
    profiles.add(Profile(account="A", user="me", role="ADMIN"))
    assert [p.role for p in profiles.load()] == ["ADMIN"]


def test_a_changed_database_selection_updates_rather_than_duplicates(tmp_path) -> None:
    profiles = store(tmp_path)
    profiles.add(Profile(account="A", user="me", databases=("MYDB",)))
    profiles.add(Profile(account="A", user="me", databases=("MYDB", "OTHERDB")))
    assert [p.databases for p in profiles.load()] == [("MYDB", "OTHERDB")]


def test_a_changed_name_updates_rather_than_duplicates(tmp_path) -> None:
    profiles = store(tmp_path)
    profiles.add(Profile(account="A", user="me", name="Prod"))
    profiles.add(Profile(account="A", user="me", name="Prod (renamed)"))
    assert [p.name for p in profiles.load()] == ["Prod (renamed)"]


def test_the_list_is_capped(tmp_path) -> None:
    profiles = store(tmp_path)
    for index in range(15):
        profiles.add(Profile(account=f"ACCT{index}", user="me"))
    assert len(profiles.load()) == 10
    assert profiles.load()[0].account == "ACCT14"


def test_removing_an_entry(tmp_path) -> None:
    profiles = store(tmp_path)
    profiles.add(Profile(account="A", user="me"))
    profiles.add(Profile(account="B", user="me"))
    profiles.remove(Profile(account="A", user="me"))
    assert [p.account for p in profiles.load()] == ["B"]


def test_malformed_entries_are_skipped_not_fatal(tmp_path) -> None:
    path = tmp_path / "recent.json"
    path.write_text(
        json.dumps([{"account": "A"}, "nonsense", {"account": "B", "user": "me"}])
    )
    assert [p.account for p in ProfileStore(path).load()] == ["B"]


def test_a_missing_databases_key_defaults_to_empty(tmp_path) -> None:
    """A recent.json written before `databases` existed must load cleanly."""
    path = tmp_path / "recent.json"
    path.write_text(json.dumps([{"account": "A", "user": "me"}]))
    assert ProfileStore(path).load() == [Profile(account="A", user="me")]


def test_a_malformed_databases_value_is_ignored_not_fatal(tmp_path) -> None:
    path = tmp_path / "recent.json"
    path.write_text(json.dumps([{"account": "A", "user": "me", "databases": "MYDB"}]))
    assert ProfileStore(path).load() == [Profile(account="A", user="me", databases=())]


def test_databases_round_trip_through_json(tmp_path) -> None:
    profiles = store(tmp_path)
    profiles.add(Profile(account="A", user="me", databases=("MYDB", "OTHERDB")))
    assert profiles.load()[0].databases == ("MYDB", "OTHERDB")


def test_a_missing_name_key_defaults_to_empty(tmp_path) -> None:
    """A recent.json written before `name` existed must load cleanly."""
    path = tmp_path / "recent.json"
    path.write_text(json.dumps([{"account": "A", "user": "me"}]))
    assert ProfileStore(path).load() == [Profile(account="A", user="me")]


def test_the_name_round_trips_through_json(tmp_path) -> None:
    profiles = store(tmp_path)
    profiles.add(Profile(account="A", user="me", name="Prod - Analyst"))
    assert profiles.load()[0].name == "Prod - Analyst"


def test_the_label_shows_account_user_and_role() -> None:
    assert Profile(account="ACME", user="me", role="ADMIN").label == "ACME / me (ADMIN)"
    assert Profile(account="ACME", user="me").label == "ACME / me"


def test_the_label_shows_the_name_when_given() -> None:
    """The whole point of naming a profile: it replaces the account/user/role
    label in the Recent list, not just supplements it."""
    assert Profile(account="ACME", user="me", role="ADMIN", name="Prod").label == "Prod"


def test_a_blank_name_falls_back_to_the_account_user_label() -> None:
    assert Profile(account="ACME", user="me", name="").label == "ACME / me"


def test_params_round_trip_without_the_password() -> None:
    entry = Profile(account="A", user="me", role="R", warehouse="W")
    params = entry.to_params()
    assert params.role == "R"
    assert params.warehouse == "W"
    assert params.password is None
    assert Profile.from_params(params) == entry


def test_blank_role_and_warehouse_become_none_in_params() -> None:
    """Empty strings would be sent to the driver as real values."""
    params = Profile(account="A", user="me").to_params()
    assert params.role is None
    assert params.warehouse is None
