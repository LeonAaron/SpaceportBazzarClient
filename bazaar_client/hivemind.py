"""Run the upstream thin bridge using the assignment client's configuration."""

from bazaar_client.config import ClientConfig


async def run_hivemind_mode(config: ClientConfig) -> int:
    try:
        from spaceport_hivemind.client import HiveConfig, HivemindClient
        from spaceport_hivemind.game import GameConfig
    except ImportError as exc:
        raise RuntimeError(
            "Hivemind mode requires: git submodule update --init; "
            "python -m pip install -e ./spaceport_hivemind"
        ) from exc

    game = GameConfig(config.ws_url, config.station_id, config.token.reveal())
    hive = HiveConfig(
        config.hivemind_endpoint,
        config.hivemind_key.reveal() if config.hivemind_key else None,
    )
    await HivemindClient(game, hive).run()
    return 0
