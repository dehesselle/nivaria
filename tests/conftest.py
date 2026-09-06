from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def colour_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep NO_COLOR out of the way.

    It puts Textual's monochrome filter into the render path, which behaves
    differently enough to make UI assertions flaky depending on the shell the
    suite happens to run from.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point every store at tmp_path so tests never touch the real config.

    Settings need no monkeypatching any more: `tcw`'s store resolves its path
    lazily, so `configure(path=...)` is enough and it takes effect for every
    module that already imported `config` by name.
    """
    from tcw.settings import configure

    from snowcone import paths

    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        paths, "settings_file", lambda: tmp_path / "config" / "settings.json"
    )
    monkeypatch.setattr(
        paths, "recent_file", lambda: tmp_path / "config" / "recent.json"
    )
    # cache_file() is not patched directly: it takes (account, role) now, and
    # its own implementation already builds on cache_dir(), patched above, so
    # isolating that one function is enough to keep it under tmp_path too.

    configure(path=tmp_path / "config" / "settings.json")
    yield
    # Put the real app config back, so a later non-test import of
    # snowcone.settings is not left pointing at a deleted tmp_path.
    configure(paths.APP_NAME)
