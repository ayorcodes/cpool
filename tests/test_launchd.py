"""launchd runs with a bare PATH; warm failed every time because it couldn't find `claude`."""
from pathlib import Path

from cpool import launchd
from cpool.config import Account, Config, Pool


def test_plists_put_claude_and_cpool_dirs_on_launchd_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        launchd.shutil, "which", lambda n: "/opt/fake/bin/claude" if n == "claude" else None
    )
    cfg = Config(Pool(start_hour=8), (Account("a0", Path("/x/0"), 1),))

    plists = launchd.install(cfg, "/x/bin/cpool", dry_run=True)

    assert set(plists) == {launchd.WARM, launchd.POLL}
    for plist in plists.values():
        parts = plist["EnvironmentVariables"]["PATH"].split(":")
        assert {"/opt/fake/bin", "/x/bin", "/usr/bin"} <= set(parts)
        assert len(parts) == len(set(parts))


def test_install_only_limits_written_labels(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path / "cp"))
    cfg = Config(Pool(start_hour=8), (Account("a0", Path("/x/0"), 1),))
    calls = []
    monkeypatch.setattr(launchd, "_launchctl", lambda *a: calls.append(a) or 0)
    monkeypatch.setattr(launchd, "_bootstrap", lambda d, p: calls.append(("bootstrap", p.name)))

    plists = launchd.install(cfg, "/x/bin/cpool", only=(launchd.WARM,))

    assert set(plists) == {launchd.WARM}
    assert any(launchd.WARM in str(c) for c in calls)
    assert all(launchd.POLL not in str(c) for c in calls)
