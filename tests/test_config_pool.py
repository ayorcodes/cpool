import pytest

from cpool import config


def test_pool_hard_defaults_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    config.save_account(config.Account("a", tmp_path / "a", 1))
    cfg = config.load()
    assert cfg.pool.hard == 97
    assert cfg.pool.auto_hard is True
    path = tmp_path / "config.toml"
    path.write_text(path.read_text().replace("auto_hard = true", "auto_hard = false"))
    config.save_account(config.Account("b", tmp_path / "b", 2))
    assert config.load().pool.auto_hard is False


@pytest.mark.parametrize("setting", ['auto_hard = "yes"', 'auto_hard = 1', 'hard = 120', 'hard = 0'])
def test_invalid_hard_settings(tmp_path, monkeypatch, setting):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(f"[pool]\n{setting}\n")
    with pytest.raises(config.ConfigError):
        config.load()


def test_chain_settings_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[pool]\n")
    cfg = config.load(path)
    assert (cfg.pool.strategy, cfg.pool.warm) == ("clock", "clock")
    assert 'strategy =' not in config._dump(cfg)
    assert 'warm =' not in config._dump(cfg)
    path.write_text('[pool]\nstrategy = "chain"\nwarm = "chain"\n')
    cfg = config.load(path)
    path.write_text(config._dump(cfg))
    assert config.load(path).pool == cfg.pool
    assert 'strategy = "chain"' in path.read_text()


@pytest.mark.parametrize("setting,key", [('warm = "x"', 'warm'), ('strategy = 1', 'strategy')])
def test_invalid_chain_settings(tmp_path, setting, key):
    path = tmp_path / "config.toml"
    path.write_text(f"[pool]\n{setting}\n")
    with pytest.raises(config.ConfigError, match=f"pool.{key}"):
        config.load(path)
