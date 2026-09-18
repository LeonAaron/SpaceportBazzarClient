"""The step-2 entrypoint: reporting, exit codes and phase gating.

`run` is driven against a fake session so the whole flow is checked without a
socket; the live run against the practice server is the separate proof.
"""

from __future__ import annotations

import logging

import pytest

from bazaar_client.app import CommandOutcome
from bazaar_client.cli import describe_opening_state, describe_outcome, main, run
from bazaar_client.config import ClientConfig, Secret
from bazaar_client.connection.lifecycle import SessionLifecycle
from bazaar_client.domain.types import ControlCode, Phase, Resource, ResultCode
from tests.fixtures import factories

TOKEN = "cli-secret-token"


class FakeLifecycle:
    def __init__(self, trading: bool) -> None:
        self._trading = trading

    def can_send_trading_commands(self) -> bool:
        return self._trading


class FakeIds:
    def next(self, kind: str) -> str:
        return f"{kind}-1"


class FakeSession:
    """Implements just the surface `run` touches."""

    def __init__(self, snapshot, ack, outcome, follow_up, trading=True):
        self._snapshot = snapshot
        self._ack = ack
        self._outcome = outcome
        self._follow_up = follow_up
        self.lifecycle = FakeLifecycle(trading)
        self.request_ids = FakeIds()
        self.run_id = snapshot.run_id
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def handshake(self, timeout: float = 15.0):
        return self._snapshot, self._ack

    async def send_command(self, message, kind, request_id, timeout: float = 15.0):
        self.sent.append((kind, request_id, message))
        return self._outcome

    async def wait_for_snapshot(self, min_sequence=1, timeout: float = 15.0):
        return self._follow_up


def make_config(**overrides) -> ClientConfig:
    defaults = dict(ws_url="ws://test/ws", token=Secret(TOKEN), station_id="P01")
    return ClientConfig(**{**defaults, **overrides})


def install_session(monkeypatch, session):
    monkeypatch.setattr("bazaar_client.cli.BazaarSession", lambda config: session)
    return session


def happy_session(**overrides):
    snapshot = factories.make_snapshot(snapshot_sequence=1, world_version=2)
    follow_up = factories.make_snapshot(
        snapshot_sequence=2,
        world_version=3,
        advertisements=(factories.make_advertisement(station_id="P01"),),
    )
    outcome = CommandOutcome("advertise-1", result=factories.make_result(request_id="advertise-1"))
    defaults = dict(snapshot=snapshot, ack=factories.make_readiness(run_id=snapshot.run_id),
                    outcome=outcome, follow_up=follow_up)
    return FakeSession(**{**defaults, **overrides})


# --- the happy path -------------------------------------------------------


async def test_run_completes_the_handshake_and_one_advertise(monkeypatch):
    session = install_session(monkeypatch, happy_session())

    assert await run(make_config()) == 0
    assert [kind for kind, _, _ in session.sent] == ["advertise"]


async def test_the_advertise_offers_water_and_seeks_food(monkeypatch):
    """Matches README step 2's command."""
    session = install_session(monkeypatch, happy_session())
    await run(make_config())

    _, _, message = session.sent[0]
    body = message.advertise.body

    assert list(body.selling.items) == [int(Resource.WATER)]
    assert list(body.seeking.items) == [int(Resource.FOOD)]
    assert body.expires_tick == 6


def test_advertise_ttl_respects_the_rules_cap(monkeypatch):
    """A short server TTL limit must not be exceeded."""
    import asyncio

    snapshot = factories.make_snapshot(
        rules=factories.make_rules(max_publication_ttl_ticks=2), tick=4
    )
    session = happy_session()
    session._snapshot = snapshot
    session.run_id = snapshot.run_id
    install_session(monkeypatch, session)

    asyncio.run(run(make_config()))
    _, _, message = session.sent[0]

    assert message.advertise.body.expires_tick == 6  # tick 4 + min(6, 2)


async def test_run_id_is_persisted_when_requested(monkeypatch, tmp_path):
    path = tmp_path / "run-id.txt"
    session = install_session(monkeypatch, happy_session())

    await run(make_config(run_id_file=path))

    assert path.read_text() == session.run_id


# --- gating and failure reporting ----------------------------------------


async def test_no_trading_command_is_sent_outside_a_running_phase(monkeypatch):
    """Readiness still succeeds; the command is simply withheld."""
    snapshot = factories.make_snapshot(phase=Phase.PAUSED)
    session = happy_session()
    session._snapshot = snapshot
    session.lifecycle = FakeLifecycle(trading=False)
    install_session(monkeypatch, session)

    assert await run(make_config()) == 0
    assert session.sent == []


async def test_mismatched_readiness_is_reported_as_failure(monkeypatch):
    session = happy_session()
    session._ack = factories.make_readiness(snapshot_sequence=99)
    install_session(monkeypatch, session)

    assert await run(make_config()) == 1
    assert session.sent == []


async def test_a_rejected_advertise_is_reported_as_failure(monkeypatch):
    """A failed command must not be reported as success."""
    session = happy_session()
    session._outcome = CommandOutcome(
        "advertise-1",
        result=factories.make_result(
            request_id="advertise-1", ok=False, code=ResultCode.LIMIT_REACHED
        ),
    )
    install_session(monkeypatch, session)

    assert await run(make_config()) == 1


async def test_a_protocol_error_answer_is_reported_as_failure(monkeypatch):
    session = happy_session()
    session._outcome = CommandOutcome(
        "advertise-1",
        error=factories.make_protocol_error(code=ControlCode.REQUEST_CAPACITY_EXCEEDED),
    )
    install_session(monkeypatch, session)

    assert await run(make_config()) == 1


async def test_a_missing_advertisement_in_the_new_state_is_reported(monkeypatch):
    """The result claimed success, so the follow-up state must show the listing."""
    session = happy_session()
    session._follow_up = factories.make_snapshot(snapshot_sequence=2, advertisements=())
    install_session(monkeypatch, session)

    assert await run(make_config()) == 1


# --- exit codes -----------------------------------------------------------


def test_main_reports_a_configuration_error_without_a_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("BAZAAR_TOKEN", raising=False)

    code = main(["--credentials-file", str(tmp_path / "absent.json")])

    assert code == 2
    assert "configuration error" in capsys.readouterr().err


def test_main_returns_one_when_the_session_fails(monkeypatch):
    def explode(config):
        raise RuntimeError("server rejected WebSocket connection: HTTP 401")

    monkeypatch.setattr("bazaar_client.cli.BazaarSession", explode)

    assert main(["--token", TOKEN, "--mode", "handshake"]) == 1


def test_main_returns_zero_on_success(monkeypatch):
    install_session(monkeypatch, happy_session())

    assert main(["--token", TOKEN, "--mode", "handshake"]) == 0


def test_trade_is_the_default_mode(monkeypatch):
    """The client's normal job is trading, not the step-2 demo."""
    from bazaar_client.config import config_from_args

    assert config_from_args(["--token", TOKEN]).mode == "trade"


def test_each_mode_dispatches_to_its_own_entrypoint(monkeypatch):
    called = []

    async def fake_trade(config):
        called.append("trade")
        return 0

    async def fake_walkthrough(config):
        called.append("walkthrough")
        return 0

    monkeypatch.setattr("bazaar_client.cli.run_trade_mode", fake_trade)
    monkeypatch.setattr("bazaar_client.cli.run_walkthrough_mode", fake_walkthrough)

    assert main(["--token", TOKEN, "--mode", "trade"]) == 0
    assert main(["--token", TOKEN, "--mode", "walkthrough"]) == 0
    assert called == ["trade", "walkthrough"]


def test_an_unknown_mode_is_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        main(["--token", TOKEN, "--mode", "nonsense"])


# --- reporting does not leak ---------------------------------------------


def test_state_reporting_covers_identity_rules_and_directory(caplog):
    snapshot = factories.make_snapshot(
        advertisements=(factories.make_advertisement(station_id="P02"),)
    )
    with caplog.at_level(logging.INFO, logger="bazaar_client.cli"):
        describe_opening_state(snapshot)

    text = caplog.text
    assert "station=P01" in text
    assert "specialty=WATER" in text
    assert "max_health=100" in text
    assert "P02=Verdant" in text
    assert "advertisement ad-1 from P02" in text


def test_outcome_reporting_handles_both_answer_shapes(caplog):
    with caplog.at_level(logging.INFO, logger="bazaar_client.cli"):
        describe_outcome(CommandOutcome("r1", result=factories.make_result(request_id="r1")))
        describe_outcome(
            CommandOutcome("r2", error=factories.make_protocol_error(request_id="r2"))
        )

    assert "code=OK" in caplog.text
    assert "REQUEST_CAPACITY_EXCEEDED" in caplog.text
