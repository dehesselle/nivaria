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

    `config` is a module-level singleton imported by name, so patching its
    attributes in place -- rather than reassigning snowcone.settings.config --
    is what the modules that already imported it actually see.
    """
    from snowcone import paths
    from snowcone.settings import config

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

    monkeypatch.setattr(config, "path", tmp_path / "config" / "settings.json")
    monkeypatch.setattr(config, "_values", {})
    monkeypatch.setattr(config, "_loaded", True)
