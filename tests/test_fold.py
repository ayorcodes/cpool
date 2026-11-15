"""Folding crosses CLI/statusline boundaries not covered by full-list tests."""
import io
import json
import sys

import pytest

from cpool import cli, paths, statusline, usage


_real_folded = paths.folded


@pytest.mark.parametrize("surface", ["status", "statusline"])
def test_folded_accounts_keep_active_and_pool(tmp_path, monkeypatch, capsys, surface):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "active"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("CPOOL_SHARD", raising=False)
    monkeypatch.delenv("CPOOL_STATUSLINE_NEXT", raising=False)
    monkeypatch.setattr(paths, "folded", lambda: True)
    monkeypatch.setattr(usage, "kick", lambda cfg, now: False)
    (tmp_path / "config.toml").write_text("\n".join(
        f'[[account]]\nname = "{name}"\ndir = "{tmp_path / name}"\norder = {i}\n'
        for i, name in enumerate(("active", "second", "third"))))
    (tmp_path / "state.json").write_text(json.dumps({"accounts": {
        name: {"five_hour": {"used": 20, "resets_at": 4000000000},
               "seven_day": {"used": 10, "resets_at": 4000000000},
               "tier": "default_claude_max_5x", "auth": "ok", "updated_at": 4000000000,
               "credits_enabled": False}
        for name in ("active", "second", "third")}}))
    if surface == "status":
        assert cli.main(["status"]) == 0
    else:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        assert statusline.main() == 0
    output = capsys.readouterr().out
    assert "▸ active" in output
    assert "  second" not in output
    assert "  third" not in output
    assert "  pool" in output
    if surface == "status":
        assert cli.main(["status", "-a"]) == 0
        output = capsys.readouterr().out
        assert "  second" in output and "  third" in output
        assert cli.main(["status", "--json"]) == 0
        assert [r["name"] for r in json.loads(capsys.readouterr().out)["accounts"]] == ["active", "second", "third"]


def test_fold_unfold_persist_preference(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path / "nested"))
    monkeypatch.setattr(paths, "folded", _real_folded)
    assert paths.folded()
    assert cli.main(["fold"]) == 0
    assert capsys.readouterr().out == "account rows folded (cpool unfold to show them)\n"
    assert cli.main(["unfold"]) == 0
    assert capsys.readouterr().out == "account rows shown (cpool fold to hide them)\n"
    assert paths.unfold_path().is_file()
    assert not paths.folded()
    assert cli.main(["fold"]) == 0
    assert paths.folded()
    assert not paths.unfold_path().exists()
