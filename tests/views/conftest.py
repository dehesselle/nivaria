"""The one fixture every detail-view test needs: an empty cache."""

from __future__ import annotations

import pytest

from snowcone.cache.db import Database
from snowcone.cache.repository import Repository


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "cache.db")
    assert database.prepare()
    return Repository(database)
