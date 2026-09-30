"""Remembered connection profiles.

Deliberately holds no secret: account, user, authenticator, role and warehouse
only. For `externalbrowser` and `oauth` there is no password to keep -- the
driver's own browser flow obtains and caches the credential itself, and for
`username_password_mfa` the driver's own MFA-token cache already means the
password is typed once per machine rather than once per connection -- so
snowcone never has a reason to write one, and never does.

Also carries an optional, user-typed `name` -- purely a display label, not
part of `identity`: two connects to the same account/user/authenticator are
still the same profile whether or not (or how) either was named. An unnamed
profile falls back to the account/user/role label it always used.

Also carries the databases last selected for that identity's Object tree --
UI state, not a connection parameter, riding on the same record because it is
naturally scoped to the same identity (account/user/authenticator). This is
why the type is `Profile`, not a narrower "recent connection": it already
describes more than what it takes to open a connection.

Different screens own different fields of the same record -- the connect
screen the connection fields and the name, the database picker the database
selection -- so both write through `ProfileStore.remember`, which merges into
the stored record rather than replacing it. Rebuilding the record from
`ConnectParams` alone and handing it to `add` is what once silently wiped a
profile's name every time its databases were picked.

Reads degrade silently: a missing or corrupt file, or one written before the
`databases` field existed, means "no recents"/"no remembered databases",
never a crash before the UI is even up. Writes do not follow suit for a file
that exists but cannot be read: every write is a read-modify-write, and
degrading that read to "no recents" would replace the whole file with one
entry. The write is skipped instead, leaving the file for the user to fix or
delete.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from snowcone.gateway.pool import EXTERNAL_BROWSER, ConnectParams
from snowcone.paths import read_json, recent_file, write_json_atomic

MAX_ENTRIES = 10

#: read_json's default for an unreadable file, distinguishable from any value
#: a valid file could hold.
_UNREADABLE = object()


@dataclass(frozen=True)
class Profile:
    account: str
    user: str
    authenticator: str = EXTERNAL_BROWSER
    #: A user-typed, optional display label -- never sent to the driver, and
    #: not part of `identity` (see there). Defaults to "" so a file written
    #: before this field existed degrades gracefully, the same way role/
    #: warehouse/databases already do.
    name: str = ""
    role: str = ""
    warehouse: str = ""
    #: The databases last selected for this identity's Object tree. Plays no
    #: part in to_params/from_params -- it is UI state, not a connector
    #: parameter. Defaults to () so a file written before this field existed
    #: degrades gracefully, the same way role/warehouse already do for a file
    #: with neither.
    databases: tuple[str, ...] = ()

    @property
    def identity(self) -> tuple[str, str, str]:
        """What makes two entries the same connection, for de-duplication.

        Account and user are casefolded: Snowflake resolves both
        case-insensitively, so comparing them exactly would turn a reconnect
        typed as `myorg-acct` into a second, unnamed entry beside the stored
        `MyOrg-Acct` one.
        """
        return (self.account.casefold(), self.user.casefold(), self.authenticator)

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        suffix = f" ({self.role})" if self.role else ""
        return f"{self.account} / {self.user}{suffix}"

    def to_params(self, password: str | None = None) -> ConnectParams:
        return ConnectParams(
            account=self.account,
            user=self.user,
            authenticator=self.authenticator,
            role=self.role or None,
            warehouse=self.warehouse or None,
            password=password,
        )

    @classmethod
    def from_params(cls, params: ConnectParams) -> Profile:
        # name and databases stay at their defaults -- a bare ConnectParams
        # carries neither a typed label nor a database selection to recover.
        # A caller that needs either does so explicitly, via
        # dataclasses.replace, after calling this (see ConnectScreen._finish).
        return cls(
            account=params.account,
            user=params.user,
            authenticator=params.authenticator,
            role=params.role or "",
            warehouse=params.warehouse or "",
        )


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        # Resolved lazily so a test that patches paths.recent_file after this
        # object was constructed still gets the patched location.
        return self._path if self._path is not None else recent_file()

    def load(self) -> list[Profile]:
        entries = self._read()
        return entries if entries is not None else []

    def find(self, params: ConnectParams) -> Profile | None:
        """The stored profile with `params`' identity, if any."""
        identity = Profile.from_params(params).identity
        return next((p for p in self.load() if p.identity == identity), None)

    def _read(self) -> list[Profile] | None:
        """The stored entries, or None when the file exists but cannot be
        read -- which load() degrades to "no recents" but a write must not
        (see the module docstring)."""
        if not self.path.exists():
            return []
        data = read_json(self.path, _UNREADABLE)
        if not isinstance(data, list):
            return None
        entries: list[Profile] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            raw_databases = item.get("databases", [])
            databases = (
                tuple(str(d) for d in raw_databases)
                if isinstance(raw_databases, list)
                else ()
            )
            try:
                entries.append(
                    Profile(
                        account=str(item["account"]),
                        user=str(item["user"]),
                        authenticator=str(item.get("authenticator", EXTERNAL_BROWSER)),
                        name=str(item.get("name", "")),
                        role=str(item.get("role", "")),
                        warehouse=str(item.get("warehouse", "")),
                        databases=databases,
                    )
                )
            except (KeyError, TypeError):
                continue
        return entries

    def remember(self, params: ConnectParams, **fields: object) -> None:
        """Merge `params` and `fields` into the stored profile for `params`'
        identity and promote it to the front.

        Starts from the stored record, so every field the caller does not
        pass -- the name when the picker writes, the databases when a connect
        does -- survives. The connection fields always come from `params`:
        they describe the session as it actually is now. Call only once the
        connection behind `params` is known to work.
        """
        entries = self._read()
        if entries is None:
            return
        fresh = Profile.from_params(params)
        existing = next((p for p in entries if p.identity == fresh.identity), None)
        base = (
            fresh
            if existing is None
            else replace(
                existing,
                account=fresh.account,
                user=fresh.user,
                authenticator=fresh.authenticator,
                role=fresh.role,
                warehouse=fresh.warehouse,
            )
        )
        self._promote(entries, replace(base, **fields))

    def add(self, profile: Profile) -> None:
        """Store `profile` as given, replacing any entry with its identity
        wholesale, and promote it to the front. Screens use `remember`
        instead, which keeps the fields they do not own."""
        entries = self._read()
        if entries is not None:
            self._promote(entries, profile)

    def remove(self, profile: Profile) -> None:
        entries = self._read()
        if entries is not None:
            self._write([p for p in entries if p.identity != profile.identity])

    def _promote(self, entries: list[Profile], profile: Profile) -> None:
        rest = [p for p in entries if p.identity != profile.identity]
        self._write([profile, *rest])

    def _write(self, entries: list[Profile]) -> None:
        try:
            write_json_atomic(self.path, [asdict(p) for p in entries[:MAX_ENTRIES]])
        except OSError:
            pass
