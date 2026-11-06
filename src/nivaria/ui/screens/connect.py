# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The connect screen: form on the left, recent connections on the right.

This screen owns the whole "produce a working connection pool" interaction,
including the retry loop, rather than handing parameters up and letting the app
drive it. That keeps the progress reporting local to the widget that shows it.

Opening the pool is where every MFA prompt in a nivaria session happens, so
only **one** connection is opened on submit -- there is no point prompting for
more before knowing the first one even works. If the configured pool size asks
for more, a confirmation dialog spells out how many additional connections
that means and lets the user decline, which is also what keeps a pool size of
1 always reachable (handy under test, and for a quick look that does not want
extra MFA prompts). The pool still opens connections one at a time, so the
prompts arrive one at a time too; `_on_progress` is what turns that into
"Connecting 2/5" instead of an unexplained pause with browser windows
appearing.

OAuth is the one exception to the confirmation above: its access/refresh
token is cached and reused for every connection the pool opens (see
`snowflake/credential_cache.py`), so growing the pool no longer means N-1 more
browser prompts -- there is nothing left for the user to weigh in on, and
`_offer_growth` skips straight to growing it.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from tcoda.dialogs import ConfirmDialog
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from nivaria.profiles import Profile, ProfileStore
from nivaria.snowflake.pool import (
    AUTHENTICATORS,
    EXTERNAL_BROWSER,
    OAUTH,
    USERNAME_PASSWORD_MFA,
    ConnectionPool,
    Connector,
    ConnectParams,
)
from nivaria.ui.screens.connecting import ConnectingDialog

# OAuth listed first: it is the dialog's default, ahead of the two
# longer-standing methods.
_AUTHENTICATOR_LABELS = {
    OAUTH: "OAuth",
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


class ConnectScreen(ModalScreen["ConnectionPool | None"]):
    """Collects connection details and returns a connected pool.

    A modal, not a plain Screen: opened at startup it looks the same either
    way (nothing is behind it), but reopened from the Account > Connect chord
    it dims the browser behind it instead of fully replacing it -- the same
    treatment `ConfirmDialog` and `PickerDialog` already get.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        recent: ProfileStore | None = None,
        *,
        pool_size: int = 3,
        connector: Connector | None = None,
        prefill: Profile | None = None,
    ) -> None:
        super().__init__()
        self._profile_store = recent or ProfileStore()
        self._recent: list[Profile] = self._profile_store.load()
        self._pool_size = pool_size
        self._connector = connector
        self._prefill = prefill or (self._recent[0] if self._recent else None)
        self._connecting = False
        self._connecting_screen: ConnectingDialog | None = None

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
                    # Both optional-ish and both short: the name is never
                    # required, and the authenticator always has a default,
                    # so pairing them buys back a row the same way role and
                    # warehouse do below.
                    with Horizontal(id="connect-identity"):
                        yield Input(
                            value=entry.name if entry else "",
                            placeholder="name",
                            id="name",
                        )
                        yield Select(
                            [
                                (label, value)
                                for value, label in _AUTHENTICATOR_LABELS.items()
                            ],
                            value=entry.authenticator if entry else OAUTH,
                            allow_blank=False,
                            id="authenticator",
                        )
                    yield Input(
                        value=entry.account if entry else "",
                        placeholder="account *",
                        id="account",
                    )
                    yield Input(
                        value=entry.user if entry else "",
                        placeholder="user *",
                        id="user",
                    )
                    yield Input(
                        placeholder="password *",
                        password=True,
                        id="password",
                    )
                    # Both optional and both short: pairing them buys back
                    # three rows, which is the difference between fitting an
                    # 80x24 terminal and not.
                    with Horizontal(id="connect-optional"):
                        yield Input(
                            value=entry.role if entry else "",
                            placeholder="role",
                            id="role",
                        )
                        yield Input(
                            value=entry.warehouse if entry else "",
                            placeholder="warehouse",
                            id="warehouse",
                        )
                yield Static("", id="connect-status")
                with Horizontal(id="connect-buttons"):
                    yield Button("Connect", variant="primary", id="connect-go")
                    yield Button("Cancel", id="connect-cancel")
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
            self.action_cancel()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is None:
            return
        entry = self._recent[int(event.option.id)]
        self._fill_from(entry)
        # A browser-SSO or OAuth entry has everything it needs already -- the
        # driver obtains its own credential -- so selecting one connects
        # straight away; a password entry still needs the password typed in.
        if entry.authenticator == USERNAME_PASSWORD_MFA:
            self.query_one("#password", Input).focus()
        else:
            self.submit()

    def action_cancel(self) -> None:
        if not self._connecting:
            self.dismiss(None)

    # --------------------------------------------------------------- state

    def _sync_password_enabled(self) -> None:
        """Grey out the password when the driver owns the credential.

        Leaving it enabled would invite typing a password that is then silently
        discarded: externalbrowser and OAuth each obtain their own credential
        through a browser popup, and never send one.
        """
        password = self.query_one("#password", Input)
        needs_password = self._authenticator == USERNAME_PASSWORD_MFA
        password.disabled = not needs_password
        password.placeholder = (
            "password *" if needs_password else "handled by your identity provider"
        )

    @property
    def _authenticator(self) -> str:
        value = self.query_one("#authenticator", Select).value
        return str(value) if value in AUTHENTICATORS else OAUTH

    def _value(self, field: str) -> str:
        return self.query_one(f"#{field}", Input).value.strip()

    def _fill_from(self, entry: Profile) -> None:
        self.query_one("#name", Input).value = entry.name
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
        self._profile_store.remove(self._recent[index])
        self._recent = self._profile_store.load()
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
        self._status("")
        self._set_busy(True)
        self._show_connecting(1)
        self._connect(params)

    @work(thread=True, exclusive=True, group="connect")
    def _connect(self, params: ConnectParams) -> None:
        """Open exactly one connection -- growing the pool further is only
        worth asking about once this one is known to work."""
        pool = ConnectionPool(params, 1, connector=self._connector)
        try:
            pool.connect_all(progress=self._on_progress)
        # The driver raises many unrelated types, and a browser-SSO timeout is
        # an ordinary outcome here rather than a bug.
        except Exception as error:  # noqa: BLE001
            pool.close()
            message = str(error) or error.__class__.__name__
            self.app.call_from_thread(self._failed, message)
            return
        self.app.call_from_thread(self._connected, params, pool)

    def _connected(self, params: ConnectParams, pool: ConnectionPool) -> None:
        self._hide_connecting()
        additional = self._pool_size - pool.size
        if additional < 1:
            self._finish(params, pool)
            return
        self._offer_growth(params, pool, additional)

    @work(exclusive=True, group="connect")
    async def _offer_growth(
        self, params: ConnectParams, pool: ConnectionPool, additional: int
    ) -> None:
        # OAuth's cached access/refresh token is reused for every connection
        # the pool opens (see snowflake/credential_cache.py), so growing it no
        # longer means N-1 more browser prompts -- nothing left to ask about.
        if params.authenticator != OAUTH:
            confirmed = await self.app.push_screen_wait(
                ConfirmDialog(
                    f"Do you want to expand the connection pool to {self._pool_size}?",
                    title="Connection pool",
                )
            )
            if not confirmed:
                self._finish(params, pool)
                return
        self._show_connecting(pool.size + 1)
        try:
            # to_thread, not another @work(thread=True): this coroutine is
            # already the one awaiting the confirm dialog, and splitting the
            # blocking connector calls into a second worker would gain nothing
            # but a second callback to marshal back.
            await asyncio.to_thread(pool.grow, additional, self._on_progress)
        except Exception as error:  # noqa: BLE001
            pool.close()
            message = str(error) or error.__class__.__name__
            self._failed(message)
            return
        self._hide_connecting()
        self._finish(params, pool)

    def _on_progress(self, done: int, total: int) -> None:
        """Called from a worker thread, so it has to marshal back.

        `total` (the size of whichever `connect_all`/`grow` call is under way)
        is ignored in favour of `self._pool_size`, the pool's eventual target
        -- so the dialog reads the same "(i/N)" whether it is showing the lone
        up-front connection or growth confirmed afterwards.
        """
        self.app.call_from_thread(self._update_connecting, done)

    def _show_connecting(self, index: int) -> None:
        self._connecting_screen = ConnectingDialog(index, self._pool_size)
        self.app.push_screen(self._connecting_screen)

    def _update_connecting(self, index: int) -> None:
        if self._connecting_screen is not None:
            self._connecting_screen.update_progress(index, self._pool_size)

    def _hide_connecting(self) -> None:
        if self._connecting_screen is not None:
            self._connecting_screen.dismiss()
            self._connecting_screen = None

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self._hide_connecting()
        self.notify(f"Could not connect: {message}", severity="error", timeout=8)
        self.app.bell()

    def _finish(self, params: ConnectParams, pool: ConnectionPool) -> None:
        # Recorded only now: a connection that never worked is not one worth
        # offering again next time. The name comes straight from the form
        # just submitted -- it is an editable field right here, not state
        # owned elsewhere, so whatever is currently typed (including blank,
        # if cleared) is what should stick. The database selection is not
        # this screen's to set, and `remember` keeps it.
        self._profile_store.remember(params, name=self._value("name"))
        self.dismiss(pool)

    def _on_key(self, event: events.Key) -> None:
        if self._connecting and event.key == "escape":
            event.prevent_default()
            event.stop()
