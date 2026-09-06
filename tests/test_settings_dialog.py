"""The settings dialog: a tree of groups on the left, the selected group's
settings on the right, writing edits through ConfigStore.set -- which is
what actually lands them on disk."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from textual.app import App
from textual.widgets import Button, Input, Label, Tree
from textual.widgets.tree import TreeNode

import snowcone.ui.app as app_module
from snowcone.settings import CACHE_TTL, POOL_SIZE, config
from snowcone.ui.screens.confirm import ConfirmScreen
from snowcone.ui.screens.settings_dialog import SettingsScreen


class Harness(App[None]):
    """Pushes the dialog on mount, the same way MainScreen.open_settings
    does, and records whatever it is dismissed with."""

    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def on_mount(self) -> None:
        self.push_screen(SettingsScreen())


async def settle(pilot, rounds: int = 3) -> None:
    """Waits out the deferred initial selection (`call_after_refresh`) and
    the `@work`-backed group switch/cancel handlers, neither of which lands
    within a single event-loop tick."""
    for _ in range(rounds):
        await pilot.pause()
        await asyncio.sleep(0.02)
        await pilot.pause()


def _node(tree: Tree[str], label: str) -> TreeNode[str]:
    return next(n for n in tree.root.children if str(n.label) == label)


async def test_the_cache_group_is_selected_by_default_and_lists_its_settings() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        assert screen.query_one("#value-cache_ttl", Input).value == str(
            CACHE_TTL.default
        )
        assert screen.query_one("#value-page_size", Input)


async def test_buttons_are_compact_and_ordered_apply_ok_cancel() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        buttons = list(screen.query_one("#buttons").query(Button))
        assert [b.id for b in buttons] == [
            "settings-apply",
            "settings-ok",
            "settings-cancel",
        ]
        assert all(b.compact for b in buttons)


async def test_a_labels_row_lines_up_with_its_bordered_input() -> None:
    """The label sits one row below the field's top edge, level with the
    input's own text row rather than its border -- not flush with the top of
    a 3-cell-tall bordered Input."""
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        field = screen.query_one("#value-cache_ttl", Input)
        label = field.parent.query_one(Label)
        assert label.region.y == field.region.y + 1


async def test_a_long_description_wraps_across_multiple_lines() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        description = screen.query_one(".settings-item-description")
        assert description.region.height > 1


async def test_selecting_a_different_group_shows_its_own_settings() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        tree = screen.query_one("#settings-tree", Tree)
        tree.select_node(_node(tree, "Connection"))
        await settle(pilot)
        assert screen.query_one("#value-pool_size", Input).value == str(
            POOL_SIZE.default
        )
        assert len(screen.query("#value-cache_ttl")) == 0  # Cache's fields unmounted


async def test_pressing_ok_writes_the_changed_value_to_disk() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        screen.query_one("#value-cache_ttl", Input).value = "60"
        await pilot.click("#settings-ok")
        await pilot.pause()
        assert config.get(CACHE_TTL) == 60
        assert json.loads(config.path.read_text()) == {"cache_ttl": 60}


async def test_apply_persists_without_closing_the_dialog() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        screen.query_one("#value-cache_ttl", Input).value = "60"
        await pilot.click("#settings-apply")
        await pilot.pause()
        assert config.get(CACHE_TTL) == 60
        assert app.screen is screen


async def test_clearing_the_field_resets_the_value_to_default() -> None:
    config.set(CACHE_TTL, 60)
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        screen.query_one("#value-cache_ttl", Input).value = ""
        await pilot.click("#settings-ok")
        await pilot.pause()
        assert config.get(CACHE_TTL) == CACHE_TTL.default
        assert json.loads(config.path.read_text()) == {}


async def test_an_unparsable_value_is_rejected_and_leaves_the_file_untouched() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        screen.query_one("#value-cache_ttl", Input).value = "not a number"
        await pilot.click("#settings-ok")
        await pilot.pause()
        assert app.screen is screen  # dialog stayed open
        assert config.get(CACHE_TTL) == CACHE_TTL.default
        assert not config.path.exists()


async def test_switching_group_with_unsaved_edits_asks_to_discard() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        screen.query_one("#value-cache_ttl", Input).value = "60"
        tree = screen.query_one("#settings-tree", Tree)
        tree.select_node(_node(tree, "Connection"))
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-no")
        await settle(pilot)
        # Discard declined: still showing Cache, edit still in the field.
        assert screen.query_one("#value-cache_ttl", Input).value == "60"

        tree.select_node(_node(tree, "Connection"))
        await settle(pilot)
        await pilot.click("#confirm-yes")
        await settle(pilot)
        assert screen.query_one("#value-pool_size", Input)  # now on Connection
        assert config.get(CACHE_TTL) == CACHE_TTL.default  # discarded, never applied


async def test_cancel_with_no_edits_closes_without_asking() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        await pilot.click("#settings-cancel")
        await settle(pilot)
        assert not isinstance(app.screen, SettingsScreen)


async def test_cancel_with_unsaved_edits_confirms_before_discarding() -> None:
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        screen = app.screen
        screen.query_one("#value-cache_ttl", Input).value = "60"
        await pilot.click("#settings-cancel")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#confirm-no")
        await settle(pilot)
        assert app.screen is screen  # discard declined, dialog still open

        await pilot.click("#settings-cancel")
        await settle(pilot)
        await pilot.click("#confirm-yes")
        await settle(pilot)
        assert not isinstance(app.screen, SettingsScreen)
        assert config.get(CACHE_TTL) == CACHE_TTL.default  # nothing was applied
