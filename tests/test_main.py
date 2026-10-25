# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

import subprocess
import sys

import pytest

from nivaria import __version__, main


def test_version_prints_the_version_and_exits_without_starting_the_app(
    capsys, monkeypatch
) -> None:
    monkeypatch.setattr(
        "nivaria.ui.app.run_app", lambda: pytest.fail("the app was started")
    )
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"nivaria {__version__}\n"


def test_version_does_not_import_textual_or_the_snowflake_driver() -> None:
    """In-process the test session has already imported both, so only a fresh
    interpreter can tell."""
    probe = (
        "import sys\n"
        "from nivaria import main\n"
        "try:\n"
        "    main(['--version'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "print(sorted(m for m in ('textual', 'snowflake') if m in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines()[-1] == "[]"
