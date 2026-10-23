"""The multi-select database picker: a checkbox-glyph fuzzy list plus an
All/None/Ok/Cancel button row, sibling to the single-select PickerDialog."""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.widgets import Button, Input, OptionList

import nivaria.ui.app as app_module
from nivaria.ui.screens.multipicker import MultiPickerDialog


class Harness(App[None]):
    """Pushes the dialog on mount and records whatever it is dismissed with."""

    CSS_PATH = str(Path(app_module.__file__).parent / "app.tcss")

    def __init__(self, candidates, *, current=None) -> None:
        super().__init__()
        self._candidates = candidates
        self._current = current
        self.result: list[list[str] | None] = []

    def on_mount(self) -> None:
        self.push_screen(
            MultiPickerDialog(
                self._candidates,
                title="Databases",
                placeholder="type to filter",
                current=self._current,
            ),
            self.result.append,
        )


async def settle(pilot) -> None:
    await pilot.pause()
    await pilot.pause()


def option_text(app: App, index: int) -> str:
    return (
        app.screen.query_one("#picker-results", OptionList)
        .get_option_at_index(index)
        .prompt.plain
    )


# --------------------------------------------------------------- checkboxes


async def test_every_candidate_starts_unchecked_by_default() -> None:
    app = Harness(["MYDB", "OTHERDB"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert option_text(app, 0).startswith("☐ ")
        assert option_text(app, 1).startswith("☐ ")


async def test_current_selections_start_checked() -> None:
    app = Harness(["MYDB", "OTHERDB"], current=["OTHERDB"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert option_text(app, 0).startswith("☐ ")
        assert option_text(app, 1).startswith("☑ ")


async def test_space_toggles_the_highlighted_row() -> None:
    app = Harness(["MYDB", "OTHERDB"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#picker-results", OptionList)
        options.focus()
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert option_text(app, 0).startswith("☑ ")


async def test_clicking_a_row_toggles_it_without_closing_the_dialog() -> None:
    """OptionList._on_click sets `highlighted` then calls `action_select()`
    for a click -- exercised directly here rather than through a synthesized
    mouse event, since a real click depends on exact render-time style
    metadata the compositor attaches per cell, which is what Textual's own
    _on_click reads (`event.style.meta.get("option")`) and is not something
    a pixel offset in a test can reliably reproduce; the point under test is
    that _MultiPickerResults.action_select toggles rather than posts
    OptionSelected, which is exactly the method _on_click calls."""
    app = Harness(["MYDB", "OTHERDB"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#picker-results", OptionList)
        options.highlighted = 0
        options.action_select()
        await pilot.pause()
        assert option_text(app, 0).startswith("☑ ")
        assert isinstance(app.screen, MultiPickerDialog)
        assert app.result == []


async def test_toggling_does_not_move_the_highlighted_row() -> None:
    app = Harness(["AAA", "BBB", "CCC"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#picker-results", OptionList)
        options.focus()
        options.highlighted = 1
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert options.highlighted == 1


async def test_typing_narrows_the_list_without_losing_earlier_checks() -> None:
    app = Harness(["AAA", "BBB", "CCC"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.toggle(0)  # AAA
        await pilot.pause()
        app.screen.query_one("#picker-query", Input).value = "BBB"
        await settle(pilot)
        screen.toggle(0)  # BBB, the only shown row now
        await pilot.pause()
        app.screen.query_one("#picker-query", Input).value = ""
        await settle(pilot)
        assert screen._checked == {"AAA", "BBB"}


# ------------------------------------------------------------------ buttons


async def test_the_all_button_checks_every_shown_candidate() -> None:
    app = Harness(["AAA", "BBB"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.click("#multipicker-all")
        await pilot.pause()
        assert option_text(app, 0).startswith("☑ ")
        assert option_text(app, 1).startswith("☑ ")


async def test_the_none_button_unchecks_every_shown_candidate() -> None:
    app = Harness(["AAA", "BBB"], current=["AAA", "BBB"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.click("#multipicker-none")
        await pilot.pause()
        assert option_text(app, 0).startswith("☐ ")
        assert option_text(app, 1).startswith("☐ ")


async def test_all_and_none_operate_on_the_filtered_list_not_every_candidate() -> None:
    """Filtering to a subset and hitting All only checks what's shown --
    lets a selection be built up in filtered groups."""
    app = Harness(["AAA", "BBB", "CCC"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#picker-query", Input).value = "AAA"
        await settle(pilot)
        await pilot.click("#multipicker-all")
        await pilot.pause()
        app.screen.query_one("#picker-query", Input).value = ""
        await settle(pilot)
        assert app.screen._checked == {"AAA"}


async def test_the_ok_button_confirms_the_checked_set() -> None:
    app = Harness(["AAA", "BBB", "CCC"], current=["AAA", "CCC"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.click("#multipicker-ok")
        await pilot.pause()
    assert app.result == [["AAA", "CCC"]]


async def test_the_cancel_button_dismisses_with_none() -> None:
    app = Harness(["AAA"], current=["AAA"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.click("#multipicker-cancel")
        await pilot.pause()
    assert app.result == [None]


async def test_escape_dismisses_with_none() -> None:
    app = Harness(["AAA"], current=["AAA"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result == [None]


# --------------------------------------------------------------------- enter


async def test_enter_confirms_the_dialog_even_with_the_results_list_focused() -> None:
    """The single most important regression test: OptionList's own inherited
    "enter" binding would otherwise fire action_select (toggle) first and win
    the dispatch race, so the dialog must not just toggle one more row."""
    app = Harness(["AAA", "BBB"], current=["AAA"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#picker-results", OptionList)
        options.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == [["AAA"]]


async def test_enter_in_the_input_also_confirms() -> None:
    app = Harness(["AAA", "BBB"], current=["BBB"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#picker-query", Input).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == [["BBB"]]


# ---------------------------------------------------------------------- misc


async def test_the_result_preserves_candidate_order_not_filter_order() -> None:
    app = Harness(["ZZZ", "AAA", "MMM"], current=["AAA", "ZZZ"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.click("#multipicker-ok")
        await pilot.pause()
    assert app.result == [["ZZZ", "AAA"]]


async def test_an_empty_candidate_list_shows_nothing_to_choose_from() -> None:
    app = Harness([])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#picker-empty")
        assert len(app.screen.query(OptionList)) == 0


async def test_all_none_ok_cancel_buttons_are_present() -> None:
    app = Harness(["AAA"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        button_ids = {button.id for button in app.screen.query(Button)}
    assert button_ids == {
        "multipicker-all",
        "multipicker-none",
        "multipicker-ok",
        "multipicker-cancel",
    }


async def test_the_buttons_are_narrower_than_textuals_default_min_width() -> None:
    """Regression: Textual's Button defaults to min-width: 16 even when
    compact, which left four of them spanning nearly the whole dialog with
    no visible gap for right-alignment to show."""
    app = Harness(["AAA"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        widths = {button.id: button.size.width for button in app.screen.query(Button)}
    assert all(width == 12 for width in widths.values())


async def test_the_buttons_are_right_aligned_in_the_dialog() -> None:
    app = Harness(["AAA"])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        dialog = app.screen.query_one("#dialog")
        cancel = app.screen.query_one("#multipicker-cancel", Button)
        # Dialog pads #dialog by 2 cells on each side (padding: 1 2) --
        # the last button's right edge should sit close to the dialog's own
        # right edge, not stop well short of it the way a left- or centre-
        # aligned row would.
        gap = dialog.region.right - cancel.region.right
        assert 0 <= gap <= 3
