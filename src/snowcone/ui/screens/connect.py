"""The connect screen: form on the left, recent connections on the right.

This screen owns the whole "produce a working connection pool" interaction,
including the retry loop, rather than handing parameters up and letting the app
drive it. That keeps the progress reporting local to the widget that shows it.

Opening the pool is where every MFA prompt in a snowcone session happens. The
pool opens its connections one at a time, so the prompts arrive one at a time
too; `_on_progress` is what turns that into "Connecting 2/5" instead of an
unexplained pause with browser windows appearing.
"""

from __future__ import annotations

from typing import ClassVar

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from snowcone.connections import RecentConnections, RecentEntry
from snowcone.gateway.pool import (
    AUTHENTICATORS,
    EXTERNAL_BROWSER,
    USERNAME_PASSWORD_MFA,
    ConnectionPool,
    Connector,
    ConnectParams,
)

_AUTHENTICATOR_LABELS = {
    EXTERNAL_BROWSER: "Browser SSO",
    USERNAME_PASSWORD_MFA: "Password + MFA",
}


class RecentList(OptionList):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("delete", "delete_recent", "Delete", show=False),
    ]

    def action_delete_recent(self) -> None:
        screen = self.screen
        if isinstance(screen, ConnectScreen) and self.highlighted is not None:
            screen.delete_recent(self.highlighted)


class ConnectScreen(Screen["ConnectionPool | None"]):
    """Collects connection details and returns a connected pool."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "quit_app", "Quit", show=False),
    ]

    def __init__(
        self,
        recent: RecentConnections | None = None,
        *,
        pool_size: int = 3,
        connector: Connector | None = None,
        prefill: RecentEntry | None = None,
    ) -> None:
        super().__init__()
        self._recent_store = recent or RecentConnections()
        self._recent: list[RecentEntry] = self._recent_store.load()
        self._pool_size = pool_size
        self._connector = connector
        self._prefill = prefill or (self._recent[0] if self._recent else None)
        self._connecting = False

    # -------------------------------------------------------------- compose

    def compose(self) -> ComposeResult:
        entry = self._prefill
        with Horizontal(id="connect"):
            with Vertical(id="connect-form"):
                yield Label("Connect to Snowflake", id="connect-title")
                # Scrollable, with the status line and buttons outside it, so
                # a short terminal loses field visibility rather than losing
                # the means to submit or leave.
                with VerticalScroll(id="connect-fields"):
                    yield Select(
                        [
                            (label, value)
                            for value, label in _AUTHENTICATOR_LABELS.items()
                        ],
                        value=entry.authenticator if entry else EXTERNAL_BROWSER,
                        allow_blank=False,
                        id="authenticator",
                    )
                    yield Input(
                        value=entry.account if entry else "",
                        placeholder="account (ORG-ACCOUNT)",
                        id="account",
                    )
                    yield Input(
                        value=entry.user if entry else "",
                        placeholder="user",
                        id="user",
                    )
                    yield Input(
                        placeholder="password",
                        password=True,
                        id="password",
                    )
                    # Both optional and both short: pairing them buys back
                    # three rows, which is the difference between fitting an
                    # 80x24 terminal and not.
                    with Horizontal(id="connect-optional"):
                        yield Input(
                            value=entry.role if entry else "",
                            placeholder="role (optional)",
                            id="role",
                        )
                        yield Input(
                            value=entry.warehouse if entry else "",
                            placeholder="warehouse (optional)",
                            id="warehouse",
                        )
                yield Static("", id="connect-status")
                with Horizontal(id="connect-buttons"):
                    yield Button("Connect", variant="primary", id="connect-go")
                    yield Button("Quit", id="connect-quit")
            with Vertical(id="connect-recent"):
                yield Label("Recent")
                if self._recent:
                    yield RecentList(*self._options(), id="recent-list")
                else:
                    yield Static("No recent connections", id="recent-empty")

    def _options(self) -> list[Option]:
        # Ids are stringified indices into self._recent, so they have to be
        # reissued whenever that list changes.
        return [
            Option(entry.label, id=str(index))
            for index, entry in enumerate(self._recent)
        ]

    def on_mount(self) -> None:
        self._sync_password_enabled()
        target = "account" if not self._prefill else "connect-go"
        self.query_one(f"#{target}").focus()

    # --------------------------------------------------------------- events

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "authenticator":
            self._sync_password_enabled()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "connect-go":
            self.submit()
        else:
            self.action_quit_app()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is None:
            return
        entry = self._recent[int(event.option.id)]
        self._fill_from(entry)
        # A browser-SSO entry has everything it needs already, so selecting it
        # connects straight away; a password entry still needs the password.
        if entry.authenticator == USERNAME_PASSWORD_MFA:
            self.query_one("#password", Input).focus()
        else:
            self.submit()

    def action_quit_app(self) -> None:
        if not self._connecting:
            self.dismiss(None)

    # --------------------------------------------------------------- state

    def _sync_password_enabled(self) -> None:
        """Grey out the password when the IdP owns the credentials.

        Leaving it enabled would invite typing a password that is then silently
        discarded, since externalbrowser never sends one.
        """
        password = self.query_one("#password", Input)
        needs_password = self._authenticator == USERNAME_PASSWORD_MFA
        password.disabled = not needs_password
        password.placeholder = (
            "password" if needs_password else "handled by your identity provider"
        )

    @property
    def _authenticator(self) -> str:
        value = self.query_one("#authenticator", Select).value
        return str(value) if value in AUTHENTICATORS else EXTERNAL_BROWSER

    def _value(self, field: str) -> str:
        return self.query_one(f"#{field}", Input).value.strip()

    def _fill_from(self, entry: RecentEntry) -> None:
        self.query_one("#authenticator", Select).value = entry.authenticator
        self.query_one("#account", Input).value = entry.account
        self.query_one("#user", Input).value = entry.user
        self.query_one("#role", Input).value = entry.role
        self.query_one("#warehouse", Input).value = entry.warehouse
        self._sync_password_enabled()

    def _status(self, message: str) -> None:
        self.query_one("#connect-status", Static).update(message)

    def _set_busy(self, busy: bool) -> None:
        self._connecting = busy
        self.query_one("#connect-go", Button).disabled = busy

    def delete_recent(self, index: int) -> None:
        if not 0 <= index < len(self._recent):
            return
        self._recent_store.remove(self._recent[index])
        self._recent = self._recent_store.load()
        option_list = self.query_one("#recent-list", RecentList)
        option_list.clear_options()
        if self._recent:
            option_list.add_options(self._options())
            option_list.highlighted = min(index, len(self._recent) - 1)

    # ------------------------------------------------------------- connect

    def params(self) -> ConnectParams | None:
        """Read the form, or report what is missing and return None."""
        account = self._value("account")
        user = self._value("user")
        missing = [
            name for name, value in (("Account", account), ("User", user)) if not value
        ]
        if missing:
            self._status(f"{' and '.join(missing)} required.")
            return None
        return ConnectParams(
            account=account,
            user=user,
            authenticator=self._authenticator,
            role=self._value("role") or None,
            warehouse=self._value("warehouse") or None,
            password=self._value("password") or None,
        )

    def submit(self) -> None:
        if self._connecting:
            return
        params = self.params()
        if params is None:
            return
        self._set_busy(True)
        self._status(f"Connecting 0/{self._pool_size} ...")
        self._connect(params)

    @work(thread=True, exclusive=True, group="connect")
    def _connect(self, params: ConnectParams) -> None:
        pool = ConnectionPool(params, self._pool_size, connector=self._connector)
        try:
            pool.connect_all(progress=self._on_progress)
        # The driver raises many unrelated types, and a browser-SSO timeout is
        # an ordinary outcome here rather than a bug.
        except Exception as error:  # noqa: BLE001
            pool.close()
            message = str(error) or error.__class__.__name__
            self.app.call_from_thread(self._failed, message)
            return
        self.app.call_from_thread(self._succeeded, params, pool)

    def _on_progress(self, done: int, total: int) -> None:
        """Called from the connect thread, so it has to marshal back."""
        self.app.call_from_thread(
            self._status, f"Connecting {done}/{total} ... (approve any MFA prompt)"
        )

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self._status(f"Could not connect: {message}")
        self.app.bell()

    def _succeeded(self, params: ConnectParams, pool: ConnectionPool) -> None:
        # Recorded only now: a connection that never worked is not one worth
        # offering again next time.
        self._recent_store.add(RecentEntry.from_params(params))
        self.dismiss(pool)

    def _on_key(self, event: events.Key) -> None:
        if self._connecting and event.key == "escape":
            event.prevent_default()
            event.stop()
