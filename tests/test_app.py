from __future__ import annotations

from snowcone.ui.app import SnowconeApp
from snowcone.ui.screens.main import MainScreen


async def test_the_app_opens_the_main_screen() -> None:
    app = SnowconeApp()
    async with app.run_test(size=(120, 40)):
        assert isinstance(app.screen, MainScreen)


async def test_the_main_screen_has_its_three_panes() -> None:
    app = SnowconeApp()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        assert screen.query_one("#left")
        assert screen.query_one("#tree")
        assert screen.query_one("#context")
        assert screen.query_one("#detail")


async def test_the_command_palette_binding_is_hidden_from_the_footer() -> None:
    """The footer's right corner is reserved for snowcone's own status
    indicators, so Textual's ctrl+p key must not claim it."""
    from textual.widgets import Footer

    app = SnowconeApp()
    async with app.run_test(size=(120, 40)) as pilot:
        footer = pilot.app.screen.query_one(Footer)
        assert footer.show_command_palette is False
