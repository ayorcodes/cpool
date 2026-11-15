"""Shard persistence must survive account edits; existing tests omit this table."""
from dataclasses import replace

import pytest

from cpool import config


def test_shard_round_trip_and_account_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    config.save_shard(config.Shard())
    assert "[shard]" not in path.read_text()
    assert config.load().shard == config.Shard()
    shard = config.Shard(True, "http://localhost:9000")
    config.save_shard(shard)
    config.save_account(config.Account("main", None, 1))
    assert config.load().shard == shard
    config.remove_account("main")
    assert config.load().shard == shard
    assert config.load().accounts == ()
    config.save_shard(replace(shard, enabled=False))
    assert config.load().shard == config.Shard(False, "http://localhost:9000")


@pytest.mark.parametrize("text", [
    '[shard]\nbogus = 1\n',
    'shard = true\n',
    '[shard]\nenabled = "yes"\n',
    '[shard]\nurl = 8317\n',
])
def test_invalid_shard_config_rejected(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match="shard"):
        config.load(path)
