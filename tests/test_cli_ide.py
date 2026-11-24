"""cpool ide setup: exit codes and the printed settings snippet have no other coverage."""
import json

import pytest

from cpool import cli, launchd, mirror


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path / "cpool"))
    monkeypatch.setattr(launchd, "health", lambda label: None)
    monkeypatch.setattr(cli.sys, "argv", ["cpool"])
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "nopython"))
    monkeypatch.setenv("PATH", "")
    return tmp_path, home


def _config(tmp_path, *accounts):
    blocks = [
        f'[[account]]\nname = "{name}"\ndir = "{d}"\norder = {i}\nenabled = {str(enabled).lower()}\n'
        for i, (name, d, enabled) in enumerate(accounts)
    ]
    (tmp_path / "cpool").mkdir(exist_ok=True)
    (tmp_path / "cpool" / "config.toml").write_text("\n".join(blocks))


def _wrapper(monkeypatch, d):
    d.mkdir(parents=True, exist_ok=True)
    w = d / "cpool-ide-exec"
    w.write_text("#!/bin/sh\n")
    w.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    return w


def _mirrored(home):
    d = home / ".claude"
    d.mkdir()
    mirror.link(d)
    return d


def _snippet(out):
    line = next(ln for ln in out.splitlines() if "claudeCode.claudeProcessWrapper" in ln)
    return json.loads("{" + line + "}")["claudeCode.claudeProcessWrapper"]


def test_all_pass_prints_json_snippet(env, monkeypatch, capsys):
    tmp, home = env
    _mirrored(home)
    second = tmp / "second"
    second.mkdir()
    mirror.link(second)
    _config(tmp, ("main", "default", True), ("two", second, True))
    w = _wrapper(monkeypatch, tmp / "bin")
    assert cli.main(["ide", "setup"]) == 0
    out = capsys.readouterr().out
    assert "✗" not in out
    assert _snippet(out) == str(w)
    assert not (home / "Library" / "Application Support").exists()


def test_symlink_path_preserved(env, monkeypatch, capsys):
    tmp, home = env
    _mirrored(home)
    _config(tmp, ("main", "default", True))
    target = tmp / "real" / "cpool-bin"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    link = tmp / "bin" / "cpool-ide-exec"
    link.parent.mkdir()
    link.symlink_to(target)
    monkeypatch.setenv("PATH", str(link.parent))
    assert cli.main(["ide", "setup"]) == 0
    assert _snippet(capsys.readouterr().out) == str(link)


def test_path_with_spaces_is_json_escaped(env, monkeypatch, capsys):
    tmp, home = env
    _mirrored(home)
    _config(tmp, ("main", "default", True))
    w = _wrapper(monkeypatch, tmp / "my bin")
    assert cli.main(["ide", "setup"]) == 0
    out = capsys.readouterr().out
    assert f'"{w}"' in out
    assert _snippet(out) == str(w)


def test_sibling_of_argv0_found(env, monkeypatch, capsys):
    tmp, home = env
    _mirrored(home)
    _config(tmp, ("main", "default", True))
    w = _wrapper(monkeypatch, tmp / "bin")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(cli.sys, "argv", [str(tmp / "bin" / "cpool")])
    assert cli.main(["ide", "setup"]) == 0
    assert _snippet(capsys.readouterr().out) == str(w)


def test_default_dir_unmirrored(env, monkeypatch, capsys):
    tmp, home = env
    (home / ".claude" / "projects").mkdir(parents=True)
    _config(tmp, ("main", "default", True))
    _wrapper(monkeypatch, tmp / "bin")
    assert cli.main(["ide", "setup"]) == 1
    out = capsys.readouterr().out
    assert "✗ ~/.claude: " in out and "real dir" in out
    assert "cpool adopt ~/.claude --name main" in out
    assert "✗ main:" not in out
    assert "claudeCode.claudeProcessWrapper" in out


def test_default_dir_missing(env, monkeypatch, capsys):
    tmp, _ = env
    _config(tmp, ("main", "default", True))
    _wrapper(monkeypatch, tmp / "bin")
    assert cli.main(["ide", "setup"]) == 1
    assert "✗ ~/.claude does not exist" in capsys.readouterr().out


def test_second_account_unmirrored_default_not_double_reported(env, monkeypatch, capsys):
    tmp, home = env
    _mirrored(home)
    second = tmp / "second"
    (second / "projects").mkdir(parents=True)
    _config(tmp, ("main", "default", True), ("two", second, True))
    _wrapper(monkeypatch, tmp / "bin")
    assert cli.main(["ide", "setup"]) == 1
    out = capsys.readouterr().out
    assert "✗ two: " in out
    assert "✗ main" not in out and "✗ ~/.claude" not in out


def test_no_enabled_accounts(env, monkeypatch, capsys):
    tmp, home = env
    _mirrored(home)
    _config(tmp, ("main", "default", False))
    _wrapper(monkeypatch, tmp / "bin")
    assert cli.main(["ide", "setup"]) == 1
    assert "✗ no enabled accounts" in capsys.readouterr().out


def test_wrapper_missing_omits_snippet(env, capsys):
    tmp, home = env
    _mirrored(home)
    _config(tmp, ("main", "default", True))
    assert cli.main(["ide", "setup"]) == 1
    out = capsys.readouterr().out
    assert "✗ cpool-ide-exec not found" in out
    assert "claudeCode.claudeProcessWrapper" not in out


def test_missing_poller_is_advisory(env, monkeypatch, capsys):
    tmp, home = env
    _mirrored(home)
    _config(tmp, ("main", "default", True))
    _wrapper(monkeypatch, tmp / "bin")
    assert cli.main(["ide", "setup"]) == 0
    assert "! launchd com.cpool.poll: plist missing" in capsys.readouterr().out


def test_unhealthy_poller_is_advisory(env, monkeypatch, capsys):
    tmp, home = env
    _mirrored(home)
    _config(tmp, ("main", "default", True))
    _wrapper(monkeypatch, tmp / "bin")
    plist = home / "Library" / "LaunchAgents" / f"{launchd.POLL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("")
    monkeypatch.setattr(launchd, "health", lambda label: "not loaded")
    assert cli.main(["ide", "setup"]) == 0
    assert "! launchd com.cpool.poll: not loaded" in capsys.readouterr().out


def test_invalid_config_exits_2(env, capsys):
    tmp, _ = env
    (tmp / "cpool").mkdir()
    (tmp / "cpool" / "config.toml").write_text("[[account\n")
    assert cli.main(["ide", "setup"]) == 2
    assert "invalid TOML" in capsys.readouterr().err
