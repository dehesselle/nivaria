# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD

"""The one fixture every detail-view test needs: an empty cache."""

from __future__ import annotations

import pytest

from nivaria.cache.db import Database
from nivaria.cache.repository import Repository


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)
