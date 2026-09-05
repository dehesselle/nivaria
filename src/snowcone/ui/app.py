from __future__ import annotations

from textual.app import App

from snowcone.ui.screens.main import MainScreen


class SnowconeApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "snowcone"

    def on_mount(self) -> None:
        self.push_screen(MainScreen())


def run_app() -> None:
    SnowconeApp().run()
