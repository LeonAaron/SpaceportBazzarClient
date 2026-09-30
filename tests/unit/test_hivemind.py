"""Configuration and the optional upstream bridge boundary."""

from unittest.mock import AsyncMock

import pytest

from bazaar_client.config import ClientConfig, Secret, config_from_args
from bazaar_client.hivemind import run_hivemind_mode


def test_hive_configuration_uses_flags_over_environment(monkeypatch):
    monkeypatch.setenv("BAZAAR_HIVEMIND_ENDPOINT", "ws://env:8765")
    monkeypatch.setenv("BAZAAR_HIVEMIND_KEY", "env-key")
    config = config_from_args([
        "--mode", "hivemind", "--token", "game-token", "--station-id", "P03",
        "--hivemind-endpoint", "ws://flag:8765", "--hivemind-key", "flag-key",
    ])
    assert config.mode == "hivemind"
    assert config.station_id == "P03"
    assert config.hivemind_endpoint == "ws://flag:8765"
    assert config.hivemind_key.reveal() == "flag-key"
    assert "flag-key" not in repr(config)
    assert "game-token" not in repr(config)


async def test_bridge_receives_selected_station_and_separate_credentials(monkeypatch):
    upstream = pytest.importorskip("spaceport_hivemind.client")
    captured = []

    class Bridge:
        run = AsyncMock()

        def __init__(self, game, hive):
            captured.append((game, hive))

    monkeypatch.setattr(upstream, "HivemindClient", Bridge)
    config = ClientConfig(
        "ws://game:3001/ws", Secret("game-token"), station_id="P03",
        mode="hivemind", hivemind_endpoint="ws://hive:8765",
        hivemind_key=Secret("hive-key"),
    )
    assert await run_hivemind_mode(config) == 0
    game, hive = captured[0]
    assert (game.endpoint, game.station_id, game.token) == (
        config.ws_url, "P03", "game-token",
    )
    assert (hive.endpoint, hive.shared_key) == (config.hivemind_endpoint, "hive-key")
    Bridge.run.assert_awaited_once()


def test_hive_key_from_credentials_with_explicit_overrides(tmp_path, monkeypatch):
    import json
    path = tmp_path / "private.json"
    path.write_text(json.dumps({"hivemind_key": "file-key", "players": [
        {"station_id": "P03", "token": "game-token"}]}))
    monkeypatch.delenv("BAZAAR_HIVEMIND_KEY", raising=False)
    args = ["--mode", "hivemind", "--station-id", "P03", "--credentials-file", str(path)]
    config = config_from_args(args)
    assert config.hivemind_key.reveal() == "file-key"
    assert config.token.reveal() == "game-token"
    assert "file-key" not in repr(config)
    monkeypatch.setenv("BAZAAR_HIVEMIND_KEY", "env-key")
    assert config_from_args(args).hivemind_key.reveal() == "env-key"
    assert config_from_args(args + ["--hivemind-key", "flag-key"]).hivemind_key.reveal() == "flag-key"
    assert not config.hivemind_exit_on_finish
    assert config_from_args(args + ["--hivemind-exit-on-finish"]).hivemind_exit_on_finish


@pytest.mark.parametrize("key", ["", "  ", 123, [], {}])
def test_invalid_hive_key_is_a_configuration_error(tmp_path, monkeypatch, key):
    import json
    from bazaar_client.config import ConfigurationError
    monkeypatch.delenv("BAZAAR_HIVEMIND_KEY", raising=False)
    path = tmp_path / "private.json"
    path.write_text(json.dumps({"hivemind_key": key}))
    with pytest.raises(ConfigurationError, match="nonempty string"):
        config_from_args(["--mode", "hivemind", "--token", "t", "--credentials-file", str(path)])
