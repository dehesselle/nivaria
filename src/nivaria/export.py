# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""What a CSV export is called and how its columns are separated.

Writing the file is `App.deliver_text`'s job, and choosing the table is the
screen's. See docs/DESIGN.md "Exporting a table".
"""

from __future__ import annotations

import locale
import re
import sys
from datetime import datetime

from nivaria.settings import CSV_DELIMITER, config

if sys.platform == "win32":
    import winreg

#: Names a delivery as an export, among whatever else the app delivers: Textual
#: reports every delivery to the app, and screenshots already have their toast.
EXPORT_DELIVERY = "export"

#: What may not reach a filename. A quoted identifier can hold anything,
#: including `/`, `:` and `..`.
_UNSAFE = re.compile(r"[^\w.-]")

_LITERAL_DELIMITERS = {"comma": ",", "semicolon": ";"}


def export_filename(subject: str, *, complete: bool, now: datetime) -> str:
    """The name of an export of `subject`.

    `partial_` unless the rows are known to be the whole result, because a CSV
    holds no trace of a row cap and reads as complete wherever it ends up. The
    timestamp is there because `deliver_text` overwrites an existing file
    without asking, and because the file is what nivaria held at that moment.
    """
    prefix = "" if complete else "partial_"
    return f"{prefix}{_UNSAFE.sub('_', subject)}_{now:%Y%m%d-%H%M%S}.csv"


def csv_delimiter() -> str:
    """The column separator the settings ask for."""
    choice = config.get(CSV_DELIMITER)
    return _LITERAL_DELIMITERS.get(choice) or _system_list_separator()


def _system_list_separator() -> str:
    if sys.platform == "win32":
        return _windows_list_separator()
    return _locale_list_separator()


def _windows_list_separator() -> str:
    """The Region setting Excel itself follows when opening a CSV.

    A comma when it cannot be used: Windows allows up to three characters
    there, and `csv` takes exactly one.
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Control Panel\International"
        ) as key:
            separator, _ = winreg.QueryValueEx(key, "sList")
    except OSError:
        return ","
    return separator if isinstance(separator, str) and len(separator) == 1 else ","


def _locale_list_separator() -> str:
    """A semicolon where the decimal point is a comma, as a comma separator
    would clash with the numbers.

    macOS and Linux have no list separator setting to read. `LC_NUMERIC` is set
    only for the lookup: Python leaves it at "C" on startup, and it is
    process-wide state nothing else here expects to change.
    """
    previous = locale.setlocale(locale.LC_NUMERIC)
    try:
        locale.setlocale(locale.LC_NUMERIC, "")
        decimal_point = locale.localeconv()["decimal_point"]
    except locale.Error:
        return ","
    finally:
        locale.setlocale(locale.LC_NUMERIC, previous)
    return ";" if decimal_point == "," else ","
