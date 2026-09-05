def main() -> None:
    # Imported inside main() so `snowcone --version` does not pay for Textual
    # and the Snowflake driver being imported.
    from snowcone.ui.app import run_app

    run_app()
