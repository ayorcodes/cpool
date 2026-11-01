import os
from pathlib import Path

import pytest

from cpool import accounts, config, mirror, paths


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path / "cpool"))
    return home


def test_keychain_service_matches_claude_code_naming():
    # Contract with Claude Code's Keychain item naming; a mismatch reads the wrong account's token.
    assert accounts.keychain_service(None) == "Claude Code-credentials"
    assert accounts.keychain_service("/Users/ayorcodes/.claude2") == "Claude Code-credentials-8c57e74e"
    assert accounts.keychain_service("/Users/ayorcodes/.claudeMax") == "Claude Code-credentials-98dc3dd1"


def _populate(d: Path, tag: str):
    (d / "projects" / "-proj").mkdir(parents=True)
    (d / "projects" / "-proj" / f"{tag}.jsonl").write_text(f"session {tag}\n")
    (d / "projects" / "-proj" / "same.jsonl").write_text(f"same {tag}\n")
    (d / "projects" / f"-only-{tag}").mkdir()
    (d / "projects" / f"-only-{tag}" / "x.jsonl").write_text(tag)
    (d / "settings.json").write_text(f'{{"from": "{tag}"}}')
    (d / "history.jsonl").write_text(f"h-{tag}-1\nh-{tag}-2")


def test_link_merges_without_losing_files(env):
    a, b = env / "a", env / "b"
    _populate(a, "a")
    _populate(b, "b")
    mirror.link(a)
    mirror.link(b)
    shared = paths.shared_dir()
    for d in (a, b):
        for entry in ("projects", "settings.json", "history.jsonl"):
            assert (d / entry).is_symlink() and os.path.realpath(d / entry) == str(shared.resolve() / entry)
    assert mirror.check(a) == [] or all("missing" in p for p in mirror.check(a))
    texts = {p.read_text() for p in [*shared.rglob("*"), *b.glob("*.cpool-bak-*")] if p.is_file()}
    for expected in ("session a\n", "session b\n", "same a\n", "same b\n", "a", "b",
                     '{"from": "a"}', '{"from": "b"}'):
        assert expected in texts or expected + "\n" in texts, expected
    hist = (shared / "history.jsonl").read_text().splitlines()
    assert sorted(hist) == ["h-a-1", "h-a-2", "h-b-1", "h-b-2"]
    assert mirror.link(a) == [] and mirror.link(b) == []  # idempotent


def test_adopt_move_keeps_launch_path(env):
    src = env / ".claude2"
    (src / "projects").mkdir(parents=True)
    (src / ".credentials.json").write_text("{}")
    acct = accounts.adopt(src, "second", move=True, force=True)
    target = paths.accounts_dir() / "second"
    assert src.is_symlink() and os.path.realpath(src) == str(target.resolve())
    assert (target / ".credentials.json").exists()
    assert config.load().accounts[0].dir == src
    assert acct.dir == src
    assert accounts.storage_dir(acct) == target.resolve()
    assert accounts.resolve_account(config.load(), str(src)).name == "second"
    assert accounts.resolve_account(config.load(), None) is None


def test_adopt_crash_mid_link_is_finished_by_rerun(env, monkeypatch):
    src = env / ".claude2"
    (src / "projects").mkdir(parents=True)
    real_link = mirror.link
    calls = []

    def flaky(d):
        calls.append(d)
        if len(calls) == 1:
            raise OSError("boom")
        return real_link(d)

    monkeypatch.setattr(mirror, "link", flaky)
    with pytest.raises(OSError):
        accounts.adopt(src, "second", move=True, force=True)
    assert config.load().accounts == ()  # not registered before link finished
    acct = accounts.adopt(src, "second", move=True, force=True)
    assert [a.name for a in config.load().accounts] == ["second"]
    assert (accounts.storage_dir(acct) / "projects").is_symlink()
    # Same name + same storage again is an idempotent finish, not an error.
    assert accounts.adopt(src, "second", move=True, force=True).name == "second"


def test_adopt_move_crash_between_move_and_symlink_is_rerunnable(env, monkeypatch):
    src = env / ".claude2"
    (src / "projects").mkdir(parents=True)
    (src / ".credentials.json").write_text("{}")
    real_symlink = os.symlink
    monkeypatch.setattr(os, "symlink", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        accounts.adopt(src, "second", move=True, force=True)
    monkeypatch.setattr(os, "symlink", real_symlink)
    assert not os.path.lexists(src) and (paths.accounts_dir() / "second").is_dir()
    acct = accounts.adopt(src, "second", move=True, force=True)
    assert src.is_symlink()
    assert (paths.accounts_dir() / "second" / ".credentials.json").exists()
    assert config.load().accounts[0].dir == src and acct.dir == src


def test_history_append_is_atomic_and_newline_separated(env):
    a, b = env / "a", env / "b"
    for d, text in ((a, "a1\na2"), (b, "b1\n")):
        d.mkdir()
        (d / "history.jsonl").write_text(text)
    mirror.link(a)
    shared = paths.shared_dir() / "history.jsonl"
    mirror.link(b)
    assert shared.read_text() == "a1\na2\nb1\n"
    assert not list(shared.parent.glob("*.cpool-tmp"))


def test_running_claude_guard_trips_on_add(env, monkeypatch):
    monkeypatch.setattr(accounts, "_claude_running", lambda: True)
    with pytest.raises(accounts.AccountError, match="claude process"):
        accounts.add("third")
    assert not (paths.accounts_dir() / "third").exists()
