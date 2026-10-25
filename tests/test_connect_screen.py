# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tcoda.dialogs import ConfirmDialog
from textual.app import App
from textual.widgets import Button, Input, Select, Static

import nivaria.ui.app as app_module
from nivaria.gateway.pool import (
    CONNECT_TIMEOUT_SECONDS,
    EXTERNAL_BROWSER,
    OAUTH,
    USERNAME_PASSWORD_MFA,
)
from nivaria.profiles import Profile, ProfileStore
from nivaria.ui.screens.connect import ConnectScreen, RecentList
from nivaria.ui.screens.connecting import ConnectingDialog
from tests.fakes import FakeConnector


class Harness(App[None]):
    """Goes straight to the connect screen, reusing the real stylesheet.

    CSS_PATH is resolved relative to the module that declares the App, so a
    harness living in tests/ has to spell the path out or Textual looks for
    tests/app.tcss.
    """

    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, screen: ConnectScreen) -> None:
        super().__init__()
        self._screen = screen
        self.result: object = "unset"

    def on_mount(self) -> None:
        self.push_screen(self._screen, callback=self._done)

    def _done(self, result) -> None:
        self.result = result


def make_app(tmp_path, *, recent=None, connector=None, pool_size=1) -> Harness:
    screen = ConnectScreen(
        ProfileStore(tmp_path / "recent.json"),
        pool_size=pool_size,
        connector=connector or FakeConnector(),
    )
    if recent:
        store = ProfileStore(tmp_path / "recent.json")
        for entry in reversed(recent):
            store.add(entry)
        screen = ConnectScreen(
            store, pool_size=pool_size, connector=connector or FakeConnector()
        )
    return Harness(screen)


async def settle(pilot, rounds: int = 4) -> None:
    """Let the connect worker thread finish and its result paint.

    pilot.pause() alone drains neither a thread worker nor the
    call_from_thread hop it finishes with.
    """
    for _ in range(rounds):
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()


# ------------------------------------------------------------------ layout


async def test_the_form_and_the_recents_pane_are_both_present(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        assert screen.query_one("#connect-form")
        assert screen.query_one("#connect-recent")
        for field in ("name", "account", "user", "password", "role", "warehouse"):
            assert screen.query_one(f"#{field}", Input)


async def test_an_empty_recents_pane_says_so(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        assert pilot.app.screen.query_one("#recent-empty", Static)


# ---------------------------------------------------------- authenticator


async def test_browser_sso_disables_the_password_field(tmp_path) -> None:
    """externalbrowser never sends a password; an enabled box would invite
    typing one that is then silently discarded."""
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        assert pilot.app.screen.query_one("#password", Input).disabled is True


async def test_choosing_password_auth_enables_the_password_field(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one(
            "#authenticator", Select
        ).value = USERNAME_PASSWORD_MFA
        await pilot.pause()
        assert pilot.app.screen.query_one("#password", Input).disabled is False


async def test_oauth_is_the_default_authenticator(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        assert pilot.app.screen.query_one("#authenticator", Select).value == OAUTH


async def test_oauth_also_disables_the_password_field(tmp_path) -> None:
    """OAuth's authorization-code flow obtains its own credential through a
    browser popup, the same as externalbrowser -- there is no field for it to
    fill in, so it must not leave the password box open to a stray typed
    value either."""
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#authenticator", Select).value = OAUTH
        await pilot.pause()
        assert pilot.app.screen.query_one("#password", Input).disabled is True


# -------------------------------------------------------------- validation


async def test_submitting_an_empty_form_names_what_is_missing(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.submit()
        await pilot.pause()
        status = pilot.app.screen.query_one("#connect-status", Static)
        assert "Account and User required" in str(status.render())


async def test_a_missing_user_is_named_on_its_own(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.submit()
        await pilot.pause()
        status = pilot.app.screen.query_one("#connect-status", Static)
        assert "User required" in str(status.render())


async def test_nothing_is_connected_when_validation_fails(tmp_path) -> None:
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.submit()
        await settle(pilot)
        assert connector.calls == []


# ----------------------------------------------------------------- connect


async def test_submitting_shows_a_small_connecting_dialog(tmp_path) -> None:
    """The inline "Connecting ..." status text was easy to miss; a modal that
    covers the form is not."""
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        assert isinstance(pilot.app.screen, ConnectingDialog)
        await settle(pilot)


def test_the_connecting_dialog_blocks_the_cancel_action() -> None:
    """There is nothing a cancel could actually stop -- the blocking connect
    call cannot be interrupted -- so escape must not dismiss it."""
    assert ConnectingDialog(1, 1).check_action("cancel", ()) is False


def test_the_connecting_message_counts_against_the_pool_target() -> None:
    """The first, up-front connection and any confirmed growth share the same
    "(i/N)" wording, counted against the pool's eventual target -- not
    whatever `connect_all`/`grow` call happens to be in flight."""
    assert ConnectingDialog.message(1, 3) == "Connecting (1/3) ..."
    assert ConnectingDialog.message(2, 3) == "Connecting (2/3) ..."


async def test_the_connecting_dialog_shows_the_approval_hint() -> None:
    """Mounted standalone rather than via a real submit(): the fake connector
    can finish -- and the dialog dismiss itself -- before a test gets to look
    at it."""

    class DialogHarness(App[None]):
        def on_mount(self) -> None:
            self.push_screen(ConnectingDialog(1, 3))

    app = DialogHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        hint = str(app.screen.query_one("#connecting-hint", Static).render())
        assert "wait for browser / MFA" in hint
        assert str(CONNECT_TIMEOUT_SECONDS) in hint


async def test_the_countdown_ticks_down_and_never_goes_negative() -> None:
    """`_tick()` is called directly rather than waiting on the real 1-second
    interval, the same way other tests in this file avoid relying on real
    timing (see `settle()`)."""

    class DialogHarness(App[None]):
        def on_mount(self) -> None:
            self.push_screen(ConnectingDialog(1, 1))

    app = DialogHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectingDialog)
        screen._tick()
        assert screen._remaining == CONNECT_TIMEOUT_SECONDS - 1
        for _ in range(CONNECT_TIMEOUT_SECONDS + 5):
            screen._tick()
        assert screen._remaining == 0


async def test_update_progress_resets_the_countdown_for_the_next_connection() -> None:
    """Each connection in a connect_all/grow sequence gets its own fresh
    approval window -- a later attempt must not inherit an already-drained
    countdown from the one before it."""

    class DialogHarness(App[None]):
        def on_mount(self) -> None:
            self.push_screen(ConnectingDialog(1, 3))

    app = DialogHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectingDialog)
        screen._tick()
        screen._tick()
        assert screen._remaining == CONNECT_TIMEOUT_SECONDS - 2
        screen.update_progress(2, 3)
        assert screen._remaining == CONNECT_TIMEOUT_SECONDS


async def test_a_successful_connect_opens_only_one_connection_up_front(
    tmp_path,
) -> None:
    """The rest are only worth prompting for once the first is known to
    work."""
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=3)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#authenticator", Select).value = EXTERNAL_BROWSER
        await pilot.pause()
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
        assert len(connector.calls) == 1
        assert isinstance(pilot.app.screen, ConfirmDialog)


async def test_the_growth_confirmation_offers_yes_and_no(tmp_path) -> None:
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=3)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#authenticator", Select).value = EXTERNAL_BROWSER
        await pilot.pause()
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ConfirmDialog)
        yes_button = screen.query_one("#confirm-yes", Button)
        no_button = screen.query_one("#confirm-no", Button)
        assert str(yes_button.label) == "Yes"
        assert str(no_button.label) == "No"
        assert yes_button.has_focus


async def test_the_growth_confirmation_names_the_pools_target_size(
    tmp_path,
) -> None:
    """N is the pool's configured target -- not the number of additional
    connections that would open -- so it reads the same number the "(i/N)"
    connecting dialogs count against."""
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=3)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#authenticator", Select).value = EXTERNAL_BROWSER
        await pilot.pause()
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ConfirmDialog)
        message = str(screen.query_one("#confirm-message", Static).render())
        assert "3" in message
        await pilot.press("tab", "enter")
        await settle(pilot)


async def test_confirming_growth_returns_a_pool_of_the_configured_size(
    tmp_path,
) -> None:
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=3)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#authenticator", Select).value = EXTERNAL_BROWSER
        await pilot.pause()
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
    assert len(connector.calls) == 3
    assert app.result is not None
    assert app.result.size == 3


async def test_declining_growth_returns_a_pool_of_just_the_one_connection(
    tmp_path,
) -> None:
    """Also what keeps a pool size of 1 always reachable, e.g. under test."""
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=3)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#authenticator", Select).value = EXTERNAL_BROWSER
        await pilot.pause()
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
        await pilot.press("tab", "enter")  # move focus to Cancel, then press it
        await settle(pilot)
    assert len(connector.calls) == 1
    assert app.result is not None
    assert app.result.size == 1


async def test_oauth_growth_skips_the_confirmation_dialog(tmp_path) -> None:
    """OAuth's cached access/refresh token is reused for every connection the
    pool opens, so growing it no longer means N-1 more browser prompts --
    there is nothing left to confirm, unlike browser SSO and password+MFA
    above."""
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=3)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
    assert len(connector.calls) == 3
    assert app.result is not None
    assert app.result.size == 3


async def test_a_pool_of_exactly_one_is_never_asked_about(tmp_path) -> None:
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=1)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
    assert len(connector.calls) == 1
    assert app.result is not None
    assert app.result.size == 1


async def test_a_successful_connect_is_recorded_as_recent(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
    stored = ProfileStore(tmp_path / "recent.json").load()
    assert [e.account for e in stored] == ["ACME"]


async def test_a_typed_name_is_recorded_with_the_profile(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#name", Input).value = "Prod - Analyst"
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
    stored = ProfileStore(tmp_path / "recent.json").load()
    assert stored[0].name == "Prod - Analyst"


async def test_an_unnamed_connect_is_recorded_with_no_name(tmp_path) -> None:
    """No name typed -- same behaviour as before this field existed."""
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
    stored = ProfileStore(tmp_path / "recent.json").load()
    assert stored[0].name == ""


async def test_a_named_recent_shows_its_name_in_the_list(tmp_path) -> None:
    app = make_app(
        tmp_path,
        recent=[Profile(account="ACME", user="me", role="ADMIN", name="Prod")],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        options = pilot.app.screen.query_one("#recent-list", RecentList)
        label = str(options.get_option_at_index(0).prompt)
    assert label == "Prod"


async def test_a_plain_reconnect_preserves_the_previously_remembered_databases(
    tmp_path,
) -> None:
    """The rearchitecture from RecentEntry to Profile must not introduce a
    regression: Profile.from_params never populates `databases` (a bare
    ConnectParams carries none), and ProfileStore.add replaces the whole
    record for a matching identity -- so a naive rename would mean every
    ordinary reconnect silently wipes out whatever database selection
    MainScreen._remember_databases previously wrote for this identity, even
    though no database picking happens in this dialog at all."""
    store = ProfileStore(tmp_path / "recent.json")
    store.add(Profile(account="ACME", user="me", databases=("MYDB", "OTHERDB")))
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
    stored = ProfileStore(tmp_path / "recent.json").load()
    assert stored[0].databases == ("MYDB", "OTHERDB")


async def test_a_failed_connect_is_not_recorded(tmp_path) -> None:
    """A connection that never worked is not one worth offering next time."""
    app = make_app(tmp_path, connector=FakeConnector(fail_after=0))
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
        assert ProfileStore(tmp_path / "recent.json").load() == []


async def test_a_failed_connect_reports_why_as_a_toast_and_re_enables_the_button(
    tmp_path,
) -> None:
    app = make_app(tmp_path, connector=FakeConnector(fail_after=0))
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
        messages = [toast.message for toast in pilot.app._notifications]
        assert any("Could not connect" in message for message in messages)
        assert pilot.app.screen.query_one("#connect-go", Button).disabled is False


async def test_a_failed_connect_dismisses_the_connecting_dialog(tmp_path) -> None:
    """The little "connecting" modal must not outlive the attempt it reports
    on -- that would strand the user behind an un-cancellable dialog."""
    app = make_app(tmp_path, connector=FakeConnector(fail_after=0))
    async with app.run_test(size=(120, 40)) as pilot:
        pilot.app.screen.query_one("#account", Input).value = "ACME"
        pilot.app.screen.query_one("#user", Input).value = "me"
        pilot.app.screen.submit()
        await settle(pilot)
        assert isinstance(pilot.app.screen, ConnectScreen)


async def test_progress_is_reported_against_the_pool_target(tmp_path) -> None:
    """Opening the pool is where every MFA prompt happens, so an unexplained
    pause is exactly what this has to avoid. The first, up-front connection
    and any confirmed growth are numbered against the same target throughout
    -- not however many connections that particular step happens to open."""
    seen: list[int] = []
    app = make_app(tmp_path, pool_size=3)

    original = ConnectScreen._update_connecting

    def spy(self, index: int) -> None:
        seen.append(index)
        original(self, index)

    ConnectScreen._update_connecting = spy
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            pilot.app.screen.query_one(
                "#authenticator", Select
            ).value = EXTERNAL_BROWSER
            await pilot.pause()
            pilot.app.screen.query_one("#account", Input).value = "ACME"
            pilot.app.screen.query_one("#user", Input).value = "me"
            pilot.app.screen.submit()
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
    finally:
        ConnectScreen._update_connecting = original

    assert seen == [1, 2, 3]


async def test_the_password_is_sent_only_for_password_auth(tmp_path) -> None:
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=1)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        screen.query_one("#authenticator", Select).value = USERNAME_PASSWORD_MFA
        await pilot.pause()
        screen.query_one("#account", Input).value = "ACME"
        screen.query_one("#user", Input).value = "me"
        screen.query_one("#password", Input).value = "s3cret"
        screen.submit()
        await settle(pilot)
    assert connector.calls[0]["password"] == "s3cret"


async def test_oauth_submits_with_no_typed_secret(tmp_path) -> None:
    """OAuth (the default) needs nothing beyond account/user -- the driver's
    own browser flow supplies the credential, so there is no secret field
    standing between a fresh account/user and Connect."""
    connector = FakeConnector()
    app = make_app(tmp_path, connector=connector, pool_size=1)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        screen.query_one("#account", Input).value = "ACME"
        screen.query_one("#user", Input).value = "me"
        screen.submit()
        await settle(pilot)
    assert connector.calls[0]["authenticator"] == OAUTH
    assert "password" not in connector.calls[0]


# ----------------------------------------------------------------- recents


async def test_recents_are_listed(tmp_path) -> None:
    app = make_app(
        tmp_path,
        recent=[
            Profile(account="A", user="me", role="R"),
            Profile(account="B", user="you"),
        ],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        options = pilot.app.screen.query_one("#recent-list", RecentList)
        assert options.option_count == 2


async def test_the_form_is_prefilled_from_the_most_recent(tmp_path) -> None:
    app = make_app(
        tmp_path, recent=[Profile(account="A", user="me", role="R", name="Prod")]
    )
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        assert screen.query_one("#name", Input).value == "Prod"
        assert screen.query_one("#account", Input).value == "A"
        assert screen.query_one("#user", Input).value == "me"
        assert screen.query_one("#role", Input).value == "R"


async def test_selecting_a_browser_sso_recent_connects_immediately(tmp_path) -> None:
    """A browser-SSO entry already has everything it needs, so a quick
    browsing tool should not make you press Connect as well."""
    connector = FakeConnector()
    app = make_app(
        tmp_path,
        recent=[Profile(account="B", user="you", authenticator=EXTERNAL_BROWSER)],
        connector=connector,
        pool_size=1,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        options = pilot.app.screen.query_one("#recent-list", RecentList)
        options.focus()
        options.highlighted = 0
        await pilot.press("enter")
        await settle(pilot)
    assert connector.calls
    assert connector.calls[0]["account"] == "B"


async def test_selecting_a_password_recent_waits_for_the_password(tmp_path) -> None:
    connector = FakeConnector()
    app = make_app(
        tmp_path,
        recent=[Profile(account="B", user="you", authenticator=USERNAME_PASSWORD_MFA)],
        connector=connector,
        pool_size=1,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        options = pilot.app.screen.query_one("#recent-list", RecentList)
        options.focus()
        options.highlighted = 0
        await pilot.press("enter")
        await settle(pilot)
        assert connector.calls == []
        assert pilot.app.screen.query_one("#password", Input).has_focus


async def test_selecting_an_oauth_recent_connects_immediately(tmp_path) -> None:
    """Like browser SSO, an OAuth entry already has everything it needs -- the
    driver obtains its own credential -- so it should not wait on a field."""
    connector = FakeConnector()
    app = make_app(
        tmp_path,
        recent=[Profile(account="B", user="you", authenticator=OAUTH)],
        connector=connector,
        pool_size=1,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        options = pilot.app.screen.query_one("#recent-list", RecentList)
        options.focus()
        options.highlighted = 0
        await pilot.press("enter")
        await settle(pilot)
    assert connector.calls
    assert connector.calls[0]["account"] == "B"


async def test_deleting_a_recent_removes_it_from_the_list_and_the_file(
    tmp_path,
) -> None:
    app = make_app(
        tmp_path,
        recent=[
            Profile(account="A", user="me"),
            Profile(account="B", user="me"),
        ],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        options = pilot.app.screen.query_one("#recent-list", RecentList)
        options.focus()
        options.highlighted = 0
        await pilot.press("delete")
        await pilot.pause()
        assert options.option_count == 1
    assert [e.account for e in ProfileStore(tmp_path / "recent.json").load()] == ["B"]


# --------------------------------------------------------------- quitting


async def test_quitting_returns_no_pool(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


# ------------------------------------------------------------------ fitting


def fits(widget, screen) -> bool:
    region = widget.region
    return (
        region.x >= 0
        and region.y >= 0
        and region.right <= screen.size.width
        and region.bottom <= screen.size.height
    )


@pytest.mark.parametrize("size", [(80, 24), (64, 16), (100, 30), (120, 40)])
async def test_the_dialog_fits_the_terminal(tmp_path, size) -> None:
    """Regression: a fixed 90x24 dialog was clipped on both axes in an 80x24
    terminal -- the account box was cut off and neither button was reachable."""
    app = make_app(tmp_path)
    async with app.run_test(size=size) as pilot:
        for _ in range(3):
            await pilot.pause()
        screen = pilot.app.screen
        assert fits(screen.query_one("#connect"), screen)


@pytest.mark.parametrize("size", [(80, 24), (64, 16), (120, 40)])
async def test_both_buttons_stay_on_screen(tmp_path, size) -> None:
    """A dialog you cannot submit or leave is worse than an ugly one, so the
    buttons sit outside the scrollable field area."""
    app = make_app(tmp_path)
    async with app.run_test(size=size) as pilot:
        for _ in range(3):
            await pilot.pause()
        screen = pilot.app.screen
        for button_id in ("#connect-go", "#connect-cancel"):
            button = screen.query_one(button_id, Button)
            assert button.display
            assert fits(button, screen), f"{button_id} off screen at {size}"


async def test_the_fields_scroll_when_they_do_not_fit(tmp_path) -> None:
    """The short-terminal fallback: lose field visibility, never the means to
    act on the dialog."""
    from textual.containers import VerticalScroll

    app = make_app(tmp_path)
    async with app.run_test(size=(64, 16)) as pilot:
        for _ in range(3):
            await pilot.pause()
        fields = pilot.app.screen.query_one("#connect-fields", VerticalScroll)
        assert fields.max_scroll_y > 0


async def test_every_field_is_still_reachable_on_a_short_terminal(tmp_path) -> None:
    app = make_app(tmp_path)
    async with app.run_test(size=(64, 16)) as pilot:
        for _ in range(3):
            await pilot.pause()
        screen = pilot.app.screen
        for field in ("name", "account", "user", "password", "role", "warehouse"):
            assert screen.query_one(f"#{field}", Input)
