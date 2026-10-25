# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# Run a command against the sibling ../tcoda checkout instead of the locked
# PyPI release, e.g. `just local-tcoda python -m pytest` or
# `just local-tcoda nivaria`. Plain `pytest` would miss the repo root on
# sys.path, which the tests need for `tests.fakes`.
local-tcoda *args:
    uv run --with-editable ../tcoda {{args}}
