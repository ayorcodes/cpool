import json

from cpool import handoff, paths


def test_pending_agents_and_kept_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    paths.agents_dir().mkdir()
    (paths.agents_dir() / "r.json").write_text(json.dumps({"version": 1, "agents": {
        "a": {"agentId": "a", "prompt": "brief", "done": False},
        "b": {"agentId": "b", "done": True}}, "deferred": 0}))
    assert handoff.pending_agents("r") == [{"agentId": "a", "prompt": "brief"}]
    h = {"version": 1, "run": "r", "session_id": "s", "from": "a"}
    handoff.write(h)
    kept = handoff.keep(handoff.take("r"), "r", 1)
    assert kept == paths.handoff_dir() / "done" / "r-1.json"
    assert json.loads(kept.read_text()) == h
    assert handoff.take("r") is None


def test_pending_agents_missing_or_bad(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    assert handoff.pending_agents("r") == []
    paths.agents_dir().mkdir()
    path = paths.agents_dir() / "r.json"
    for text in ('{', '[]', '{"version":1,"agents":[]}'):
        path.write_text(text)
        assert handoff.pending_agents("r") == []
