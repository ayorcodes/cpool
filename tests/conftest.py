import pytest


@pytest.fixture(autouse=True)
def _no_codex_usage_fetch(monkeypatch):
    monkeypatch.setenv("CPOOL_CODEX_USAGE", "off")
