"""End-to-end run of the scripted exchange against a real practice server.

Each test spawns its own server on its own port with its own credential and
report files, so a run is repeatable and independent of the long-lived instance
started by docker compose.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from bazaar_client.config import ClientConfig, Secret, read_token_from_credentials
from bazaar_client.scripted_walkthrough import (
    EXPECTED_FINAL_INVENTORY,
    EXPECTED_MESSAGES_SENT,
    run_walkthrough,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bazaar-server") is None,
        reason="bazaar-server is only on PATH inside the container",
    ),
]

SERVER = "bazaar-server"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PracticeServer:
    def __init__(self, process, port: int, credentials: Path, report: Path) -> None:
        self.process = process
        self.port = port
        self.credentials = credentials
        self.report = report

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def token(self, station_id: str = "P01") -> str:
        return read_token_from_credentials(self.credentials, station_id)

    def read_report(self) -> dict:
        return json.loads(self.report.read_text())


@pytest.fixture
def practice_server(tmp_path):
    port = free_port()
    credentials = tmp_path / "validation-credentials.json"
    report = tmp_path / "validation-report.json"

    process = subprocess.Popen(
        [
            SERVER, "--codec", "protobuf",
            "--addr", f"127.0.0.1:{port}",
            "--credential-file", str(credentials),
            "--report", str(report),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode(errors="replace")
            raise RuntimeError(f"{SERVER} exited early: {stderr}")
        if credentials.exists():
            break
        time.sleep(0.1)
    else:
        process.kill()
        raise RuntimeError(f"{SERVER} did not write credentials in time")

    try:
        yield PracticeServer(process, port, credentials, report)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def config_for(server: PracticeServer, tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        ws_url=server.ws_url,
        token=Secret(server.token()),
        station_id="P01",
        log_level="INFO",
    )


async def test_scripted_walkthrough_matches_the_exercise_guide(practice_server, tmp_path):
    """Every documented counter, code and balance across the ten steps."""
    evidence_path = tmp_path / "evidence.jsonl"

    result = await run_walkthrough(config_for(practice_server, tmp_path), evidence_path)

    assert result.ok, "failed checks:\n" + "\n".join(
        f"  [{c.step}] {c.description}: {c.detail}" for c in result.failures
    )
    assert result.final_inventory == EXPECTED_FINAL_INVENTORY
    assert result.transaction_count == 2
    assert result.stored_results == 5
    assert result.messages_sent == EXPECTED_MESSAGES_SENT


async def test_server_report_confirms_the_exchange_completed(practice_server, tmp_path):
    """The server's own record, which is independent of our assertions."""
    await run_walkthrough(config_for(practice_server, tmp_path), tmp_path / "e.jsonl")

    report = practice_server.read_report()

    assert report["status"] == "sample exchange completed"
    assert report["last_completed_step"] == 10
    assert report["mismatch"] is None
    assert report["final_inventory"] == {"water": 28, "food": 31, "components": 31}


async def test_evidence_log_links_decisions_to_results_and_states(practice_server, tmp_path):
    """Logs must connect input state, command, result and the confirming snapshot."""
    evidence_path = tmp_path / "evidence.jsonl"

    result = await run_walkthrough(config_for(practice_server, tmp_path), evidence_path)
    assert result.ok

    records = [json.loads(line) for line in evidence_path.read_text().splitlines() if line.strip()]
    by_step = {r["step"]: r for r in records}

    # One record per command plus the sync.
    assert len(records) == 7

    advertise = by_step["2"]
    assert advertise["action_kind"] == "advertise"
    assert advertise["request_id"] == "student-advertise-1"
    assert advertise["observed_snapshot_sequence"] == 1
    assert advertise["result_code"] == "OK"
    assert advertise["confirmed_snapshot_sequence"] == 2
    assert advertise["inventory_after"] == {"water": 30, "food": 30, "components": 30}

    accept = by_step["7"]
    assert accept["transaction_id"]
    assert accept["inventory_after"] == {"water": 28, "food": 31, "components": 31}

    refused = by_step["9"]
    assert refused["result_ok"] is False
    assert refused["result_code"] == "REQUEST_CAPACITY_EXCEEDED"

    assert all("token" not in json.dumps(r).lower() for r in records)


async def test_a_second_connection_repeats_the_readiness_exchange(practice_server, tmp_path):
    """Readiness is required on every connection, including reconnects."""
    from bazaar_client.app import BazaarSession

    config = config_for(practice_server, tmp_path)

    async with BazaarSession(config) as first:
        snapshot, ack = await first.handshake()
        assert snapshot.snapshot_sequence == 1
        assert ack.ready

    async with BazaarSession(config) as second:
        snapshot, ack = await second.handshake()
        # A new connection restarts the per-connection counter.
        assert snapshot.snapshot_sequence == 1
        assert ack.ready
        assert snapshot.run_id == ack.run_id
