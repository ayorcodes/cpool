import pytest

from cpool import paths


@pytest.fixture(autouse=True)
def _unfold_accounts(monkeypatch):
    monkeypatch.setattr(paths, "folded", lambda: False)


@pytest.fixture(autouse=True)
def _no_codex_usage_fetch(monkeypatch):
    monkeypatch.setenv("CPOOL_CODEX_USAGE", "off")
