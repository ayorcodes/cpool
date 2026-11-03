import json
import os
import stat
import subprocess
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from cpool import advisor, cli, config, paths

KEY = "sk-ant-api03-" + "x" * 40


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


class Fake:
    def __init__(self, text="VERDICT: do it.", in_tok=1000, stop="end_turn"):
        self.text, self.in_tok, self.stop = text, in_tok, stop
        self.counted, self.streamed = [], []
        self.fail = None  # an exception get_final_message raises, as a mid-stream failure
        self.messages = SimpleNamespace(count_tokens=self._count)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _count(self, **kw):
        self.counted.append(kw)
        return SimpleNamespace(input_tokens=self.in_tok)

    def _stream(self, **kw):
        self.streamed.append(json.loads(json.dumps(kw)))
        resp = SimpleNamespace(
            model=kw["model"], stop_reason=self.stop, stop_details=None,
            content=[Block(type="thinking", thinking="hmm", signature="sig"), Block(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=2000,
                                  cache_creation_input_tokens=500, cache_read_input_tokens=0),
        )
        return _CM(resp, self.fail)


class _CM:
    def __init__(self, resp, fail=None):
        self.resp, self.fail = resp, fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        if self.fail is not None:
            raise self.fail
        return self.resp


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    fake = Fake()
    made = []

    def client(key):
        made.append(key)
        return fake

    monkeypatch.setattr(advisor, "_client", client)
    monkeypatch.setattr(advisor, "_security",
                        lambda args, stdin=None: subprocess.CompletedProcess(args, 0, KEY + "\n", ""))
    facts = tmp_path / "facts.md"
    facts.write_text("the facts")
    return SimpleNamespace(fake=fake, made=made, facts=facts, tmp=tmp_path)


def _rows():
    p = paths.advisor_dir() / "ledger.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _ask(env, *extra):
    return cli.main(["advise", "ask", "--facts", str(env.facts), "--json", *extra, "Which way?"])


# Money: the cap must block before any paid call, and every call must be reserved then settled.
def test_cap_blocks_without_call_or_reservation(env, capsys):
    advisor._append({"ts": time.time(), "kind": "final", "id": "a", "cost_usd": 99.5})
    assert _ask(env) == 4
    assert env.fake.streamed == []
    assert [r["kind"] for r in _rows()] == ["final"]
    assert "spent $99.50 of $100.00" in capsys.readouterr().err


def test_reserve_then_final_with_cost(env):
    assert _ask(env) == 0
    reserve, final = _rows()
    assert reserve["kind"] == "reserve" and final["kind"] == "final" and reserve["id"] == final["id"]
    assert reserve["worst"] == pytest.approx((1000 * 20 + 32000 * 50) / 1e6)
    assert final["cost_usd"] == pytest.approx((1000 * 10 + 2000 * 50 + 500 * 20) / 1e6)


def test_open_reservation_counts(env):
    adv = config.Advisor()
    advisor._append({"ts": time.time(), "kind": "reserve", "id": "r", "worst": 7.0})
    advisor._append({"ts": time.time() - 3 * 3600, "kind": "reserve", "id": "old", "worst": 50.0})
    advisor._append({"ts": time.time(), "kind": "reserve", "id": "done", "worst": 9.0})
    advisor._append({"ts": time.time(), "kind": "final", "id": "done", "cost_usd": 1.0})
    assert advisor.cycle(adv)["spent"] == pytest.approx(8.0)


# Money: a failure after the reservation still settles it; only a server rejection is free.
def test_midstream_failure_settles_at_worst(env, capsys):
    env.fake.fail = ConnectionError("connection dropped")
    assert _ask(env) == 2
    reserve, final = _rows()
    assert final["kind"] == "final" and final["id"] == reserve["id"]
    assert final["cost_usd"] == reserve["worst"] and final["estimated"] is True
    assert final["error"] == "ConnectionError"
    assert not (paths.advisor_dir() / "threads").exists()
    assert "connection dropped" in capsys.readouterr().err


def test_interrupt_settles_at_worst_and_reraises(env):
    env.fake.fail = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        _ask(env)
    reserve, final = _rows()
    assert final["id"] == reserve["id"] and final["cost_usd"] == reserve["worst"]


def test_server_rejection_settles_at_zero(env):
    import anthropic

    class Rejected(anthropic.APIStatusError):
        def __init__(self):
            Exception.__init__(self, "bad request")

    env.fake.fail = Rejected()
    assert _ask(env) == 2
    reserve, final = _rows()
    assert final["id"] == reserve["id"] and final["cost_usd"] == 0 and "estimated" not in final
    assert not (paths.advisor_dir() / "threads").exists()


# Money: a repeated attachment is inlined twice, so it counts twice against max_attach_bytes.
def test_duplicate_attach_counts_twice(env, capsys, monkeypatch):
    monkeypatch.setattr(config, "load", lambda *a, **k: SimpleNamespace(advisor=config.Advisor(max_attach_bytes=1500)))
    env.facts.write_text("ab")
    big = env.tmp / "big.txt"
    big.write_text("x" * 1000)
    assert _ask(env, "--attach", str(big), "--attach", str(big)) == 2
    assert env.fake.streamed == []
    err = capsys.readouterr().err
    assert "2002 bytes" in err and f"{big.resolve()}=1000" in err


# Money: the cycle boundary decides which spend counts.
def test_cycle_boundary(env):
    assert advisor.cycle_start(15, datetime(2026, 3, 10, 12)) == datetime(2026, 2, 15)
    assert advisor.cycle_start(15, datetime(2026, 1, 3)) == datetime(2025, 12, 15)
    assert advisor.cycle_start(15, datetime(2026, 3, 15, 0, 1)) == datetime(2026, 3, 15)
    adv = config.Advisor(cycle_day=15)
    now = datetime(2026, 3, 20, 12).timestamp()
    advisor._append({"ts": datetime(2026, 3, 14, 23).timestamp(), "kind": "final", "id": "x", "cost_usd": 5.0})
    advisor._append({"ts": datetime(2026, 3, 15, 1).timestamp(), "kind": "final", "id": "y", "cost_usd": 2.0})
    assert advisor.cycle(adv, now)["spent"] == pytest.approx(2.0)


# Protocol: a thread replays prior turns unchanged and enforces the round limit.
def test_thread_round_trip(env, capsys):
    assert _ask(env) == 0
    tid = json.loads(capsys.readouterr().out)["thread"]
    assert _ask(env, "--thread", tid) == 0
    sent = env.fake.streamed[1]["messages"]
    assert sent[0] == env.fake.streamed[0]["messages"][0]
    assert sent[1] == {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm", "signature": "sig"}, {"type": "text", "text": "VERDICT: do it."}]}
    assert advisor.FINAL_ROUND not in sent[2]["content"]
    assert _ask(env, "--thread", tid) == 0
    assert env.fake.streamed[2]["messages"][4]["content"].endswith(advisor.FINAL_ROUND)
    capsys.readouterr()
    assert _ask(env, "--thread", tid) == 2
    assert len(env.fake.streamed) == 3
    call = env.fake.streamed[0]
    assert call["cache_control"] == {"type": "ephemeral", "ttl": "1h"} and call["fallbacks"] == "default"
    assert call["betas"] == advisor.BETAS and "thinking" not in call


def test_needs_exit_3(env, capsys):
    env.fake.text = "NEEDS:\n- where is foo? matters\n2. what calls bar?"
    assert _ask(env) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "needs" and out["needs"] == ["where is foo? matters", "what calls bar?"]


# Security: the key reaches only the client constructor, never disk or output.
def test_secret_hygiene(env, capsys, monkeypatch):
    assert _ask(env) == 0
    out = capsys.readouterr()
    blobs = [out.out, out.err] + [p.read_text() for p in env.tmp.rglob("*") if p.is_file()]
    assert not any(KEY in b for b in blobs)
    assert env.made == [KEY]


def test_real_client_ignores_env_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "envtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "envkey")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: Bearer envtok\nx-api-key: envkey")
    c = advisor._client(KEY)
    h = {k.lower(): v for k, v in c.default_headers.items()}
    assert h["authorization"] == f"Bearer {KEY}" and "x-api-key" not in h
    assert c.api_key == KEY and c.auth_token is None
    assert str(c.base_url).rstrip("/") == advisor.BASE_URL


def test_key_write_uses_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(advisor, "_security",
                        lambda args, stdin=None: calls.append((args, stdin)) or subprocess.CompletedProcess(args, 0))
    advisor.set_key(KEY)
    (args, stdin), = calls
    assert args == ["-i"] and KEY not in " ".join(args) and KEY in stdin


# Config: unknown keys are refused; absent section gives defaults.
def test_advisor_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[advisor]\nbogus = 1\n")
    with pytest.raises(config.ConfigError):
        config.load(p)
    p.write_text("[advisor]\ncycle_day = 29\n")
    with pytest.raises(config.ConfigError):
        config.load(p)
    p.write_text("")
    assert config.load(p).advisor == config.Advisor()


# Setup must not corrupt the user's settings.
@pytest.fixture
def home(env, monkeypatch):
    h = env.tmp / "home"
    (h / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(advisor, "check_key", lambda model: None)
    return h / ".claude" / "settings.json"


def test_setup_adds_rule_and_preserves_settings(env, home, capsys):
    text = json.dumps({"model": "opus", "permissions": {"deny": ["x"], "allow": ["Read", "Bash(ls:*)"]}, "env": {"A": "1"}})
    home.write_text(text)
    assert cli.main(["advise", "setup"]) == 0
    data = json.loads(home.read_text())
    assert list(data) == ["model", "permissions", "env"]
    assert list(data["permissions"]) == ["deny", "allow"]
    assert data["permissions"]["allow"] == ["Read", "Bash(ls:*)", advisor.ALLOW_RULE]
    assert home.read_text().endswith("}\n")
    before = home.read_text()
    assert cli.main(["advise", "setup"]) == 0
    assert home.read_text() == before
    assert "allow rule: already" in capsys.readouterr().out


def test_setup_skips_invalid_settings(env, home, capsys):
    home.write_text("{not json")
    assert cli.main(["advise", "setup"]) == 0
    assert home.read_text() == "{not json"
    assert "allow rule: skipped" in capsys.readouterr().out
    home.write_text('{"permissions": {"allow": "Read"}}')
    assert cli.main(["advise", "setup"]) == 0
    assert home.read_text() == '{"permissions": {"allow": "Read"}}'


def test_remove_deletes_only_our_rule(env, home, capsys):
    home.write_text(json.dumps({"permissions": {"allow": ["Read", advisor.ALLOW_RULE, "Bash(cpool advise:ask)"]}}))
    assert cli.main(["advise", "setup"]) == 0
    skill = advisor.skill_target()
    assert skill.exists()
    skill.write_text("edited")
    assert cli.main(["advise", "setup", "--remove"]) == 0
    assert skill.exists() and "skill: skipped: modified" in capsys.readouterr().out
    assert json.loads(home.read_text())["permissions"]["allow"] == ["Read", "Bash(cpool advise:ask)"]
    assert cli.main(["advise", "setup", "--remove", "--force"]) == 0
    assert not skill.exists()


def test_shared_settings_take_precedence(env, home):
    home.write_text("{}")
    shared = paths.shared_dir() / "settings.json"
    shared.parent.mkdir(parents=True)
    shared.write_text("{}")
    assert cli.main(["advise", "setup"]) == 0
    assert advisor.ALLOW_RULE in json.loads(shared.read_text())["permissions"]["allow"]
    assert home.read_text() == "{}"


def test_setup_writes_through_a_symlinked_settings_file(env, home):
    target = env.tmp / "dotfiles" / "settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"model": "opus", "env": {"A": "1"}}))
    home.symlink_to(target)
    assert cli.main(["advise", "setup"]) == 0
    assert home.is_symlink() and home.resolve() == target.resolve()
    data = json.loads(target.read_text())
    assert advisor.ALLOW_RULE in data["permissions"]["allow"]
    assert data["model"] == "opus" and data["env"] == {"A": "1"}


def test_setup_keeps_settings_file_mode(env, home):
    home.write_text(json.dumps({"env": {"SECRET": "x"}}))
    os.chmod(home, 0o600)
    assert cli.main(["advise", "setup"]) == 0
    assert advisor.ALLOW_RULE in json.loads(home.read_text())["permissions"]["allow"]
    assert stat.S_IMODE(home.stat().st_mode) == 0o600
    assert not list(home.parent.glob(".settings.*"))
