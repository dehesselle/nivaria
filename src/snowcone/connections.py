"""Recently used connections.

Deliberately holds no secret: account, user, authenticator, role and warehouse
only. For `externalbrowser` there is no password to keep, and for
`username_password_mfa` the driver's own MFA-token cache already means the
password is typed once per machine rather than once per connection -- so
snowcone never has a reason to write one, and never does.

Reads and writes degrade silently: a missing or corrupt file means "no
recents", never a crash before the UI is even up.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from snowcone.gateway.pool import EXTERNAL_BROWSER, ConnectParams
from snowcone.paths import read_json, recent_file, write_json_atomic

MAX_ENTRIES = 10


@dataclass(frozen=True)
class RecentEntry:
    account: str
    user: str
    authenticator: str = EXTERNAL_BROWSER
    role: str = ""
    warehouse: str = ""

    @property
    def identity(self) -> tuple[str, str, str]:
        """What makes two entries the same connection, for de-duplication."""
        return (self.account, self.user, self.authenticator)

    @property
    def label(self) -> str:
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
    def from_params(cls, params: ConnectParams) -> RecentEntry:
        return cls(
            account=params.account,
            user=params.user,
            authenticator=params.authenticator,
            role=params.role or "",
            warehouse=params.warehouse or "",
        )


class RecentConnections:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        # Resolved lazily so a test that patches paths.recent_file after this
        # object was constructed still gets the patched location.
        return self._path if self._path is not None else recent_file()

    def load(self) -> list[RecentEntry]:
        data = read_json(self.path, [])
        if not isinstance(data, list):
            return []
        entries: list[RecentEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(
                    RecentEntry(
                        account=str(item["account"]),
                        user=str(item["user"]),
                        authenticator=str(item.get("authenticator", EXTERNAL_BROWSER)),
                        role=str(item.get("role", "")),
                        warehouse=str(item.get("warehouse", "")),
                    )
                )
            except (KeyError, TypeError):
                continue
        return entries

    def add(self, entry: RecentEntry) -> None:
        """Promote an entry to the front. Call only after a verified connect."""
        entries = [e for e in self.load() if e.identity != entry.identity]
        entries.insert(0, entry)
        self._write(entries)

    def remove(self, entry: RecentEntry) -> None:
        self._write([e for e in self.load() if e.identity != entry.identity])

    def _write(self, entries: list[RecentEntry]) -> None:
        try:
            write_json_atomic(self.path, [asdict(e) for e in entries[:MAX_ENTRIES]])
        except OSError:
            pass
