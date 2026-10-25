# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

import argparse

from nivaria.version import __version__


def main(argv: list[str] | None = None) -> None:
    # prog is fixed because argparse would otherwise name the program after
    # sys.argv[0], which is "__main__.py" under `python -m nivaria`.
    parser = argparse.ArgumentParser(
        prog="nivaria", description="Browse and search Snowflake objects."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.parse_args(argv)

    # Imported inside main() so `nivaria --version` does not pay for Textual
    # and the Snowflake driver being imported.
    from nivaria.ui.app import run_app

    run_app()
