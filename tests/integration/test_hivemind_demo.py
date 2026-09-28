"""Exercise our adapter against the real upstream coordinator and demo game."""

import subprocess
import sys

import pytest


def test_three_station_hivemind_demo():
    pytest.importorskip("spaceport_hivemind.demo")
    # Both projects name their distinct protobuf descriptors bazaar.proto.
    # Exercise the actual CLI import boundary in a fresh interpreter, as in use.
    script = """
import asyncio
import sys
from bazaar_client.cli import run_hivemind_mode
from bazaar_client.config import ClientConfig, Secret
from spaceport_hivemind import demo

assert 'bazaar_pb2' not in sys.modules

class AssignmentBridge:
    def __init__(self, game, hive):
        self.config = ClientConfig(
            game.endpoint, Secret(game.token), station_id=game.station_id,
            mode='hivemind', hivemind_endpoint=hive.endpoint,
            hivemind_key=Secret(hive.shared_key),
        )

    async def run(self):
        return await run_hivemind_mode(self.config)

demo.HivemindClient = AssignmentBridge
result = asyncio.run(demo.run_demo(
    duration_ticks=3, tick_duration_ms=100, timeout_seconds=5,
))
assert result.ticks == 3
assert set(result.stations) == {'P01', 'P02', 'P03'}
assert all(not station.failed for station in result.stations.values())
assert result.offers
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
