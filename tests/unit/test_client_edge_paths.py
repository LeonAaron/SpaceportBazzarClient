"""Edge and failure paths of the trading loop, supervisor, CLI and helpers.

Each test names the behaviour it protects; together they cover the branches a
healthy run rarely takes: refusals, rate limits, blocked sends, aborted
sessions, status-panel output and its failure, and unusual diagnostics.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import replace

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidHandshake, InvalidStatus
from websockets.http11 import Response

from bazaar_client import cli
from bazaar_client.app import CommandBlockedError, CommandOutcome, SessionAbortedError
from bazaar_client.autonomous import TradingLoop, TradingStats, run_trading
from bazaar_client.config import ClientConfig, Secret
from bazaar_client.diagnostics import FailureKind, diagnose
from bazaar_client.domain.types import Bundle, ControlCode, Phase, ResultCode
from bazaar_client.execution.actions import AcceptAction, OfferAction
from bazaar_client.execution.evidence import EvidenceLog, rotate
from bazaar_client.policy.decide import Decision, decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.scripted_walkthrough import Check, WalkthroughResult
from bazaar_client.strategy import get_strategy
from bazaar_client.version import working_tree_dirty
from tests.fixtures import factories as f
from tests.unit.test_executor import FakeSession, make_executor
from tests.unit.test_session_routing import FakeConnection, complete_handshake, start_session

CONFIG = ClientConfig(ws_url="ws://test/ws", token=Secret("t"), station_id="P01")


def outcome(code=ResultCode.OK, retry_after=None):
    return CommandOutcome("r-1", result=f.make_result(
        request_id="r-1", ok=code is ResultCode.OK, code=code, retry_after_tick=retry_after))


# --- the loop's own bookkeeping ---------------------------------------------


def test_refusals_are_counted_by_code_and_a_rate_limit_blocks_until_its_tick(caplog):
    loop = TradingLoop(object(), evidence=EvidenceLog())
    loop._record(AcceptAction("o-1"), outcome(ResultCode.RATE_LIMITED, retry_after=7))
    loop._record(OfferAction("P02", Bundle(water=1), Bundle.zero(), 5), outcome())

    assert (loop.stats.commands_sent, loop.stats.commands_rejected, loop.stats.gifts) == (2, 1, 1)
    assert loop.stats.rejections_by_code == {"RATE_LIMITED": 1}
    assert loop.memory.blocked_until_tick == 7
    with caplog.at_level(logging.INFO, logger="bazaar_client.autonomous"):
        loop._report(None)
    assert "rejections: {'RATE_LIMITED': 1}" in caplog.text


def test_stats_serialise_for_the_run_end_record():
    stats = TradingStats(decisions=3, accepts=1)
    stats.record_rejection("EXPIRED")

    assert stats.as_dict()["rejections_by_code"] == {"EXPIRED": 1}
    assert stats.as_dict()["decisions"] == 3


async def test_a_blocked_send_ends_the_step_without_counting_a_command():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        state = await complete_handshake(connection, session, offers=(f.make_offer(
            offer_id="gift", proposer_id="P02", recipient_id="P01", give=Bundle(food=1),
            receive=Bundle.zero(), expires_tick=50),))
        loop = TradingLoop(session, evidence=EvidenceLog())

        async def refuse(*args, **kwargs):
            raise CommandBlockedError("trading is not eligible")

        loop._executor.execute = refuse
        decision = await loop.step(state)
    finally:
        await session.stop()

    assert decision.actions and loop.stats.commands_sent == 0


def test_the_status_panel_is_logged_on_schedule_and_written_every_tick(tmp_path, caplog):
    path = tmp_path / "status.html"
    loop = TradingLoop(object(), evidence=EvidenceLog(), status_every=5, status_file=path)
    snapshot = f.make_snapshot(tick=10)
    decision, _ = decide(snapshot, PolicyMemory())

    with caplog.at_level(logging.INFO, logger="bazaar_client.autonomous"):
        loop._show_status(snapshot, decision)
        loop._show_status(snapshot, decision)  # same tick: not logged twice
        loop._show_status(replace(snapshot, tick=11), decision)  # off schedule: file only

    assert caplog.text.count("| P01 | tick 10/120") == 1
    assert "tick 11/120" in path.read_text()


def test_a_status_file_that_cannot_be_written_is_a_warning_not_a_crash(monkeypatch, caplog):
    def fail(path, panel):
        raise PermissionError("read-only")

    monkeypatch.setattr("bazaar_client.autonomous.write_status_file", fail)
    loop = TradingLoop(object(), evidence=EvidenceLog(), status_file=__import__("pathlib").Path("x"))

    with caplog.at_level(logging.WARNING, logger="bazaar_client.autonomous"):
        loop._show_status(f.make_snapshot(), Decision())

    assert "could not update status file" in caplog.text


def test_without_a_schedule_or_file_no_panel_is_built(monkeypatch):
    monkeypatch.setattr("bazaar_client.autonomous.render_status",
                        lambda *a, **k: pytest.fail("panel built for nothing"))

    TradingLoop(object(), evidence=EvidenceLog())._show_status(f.make_snapshot(), Decision())


# --- the reconnect supervisor's final paths ---------------------------------


class NoopLoop:
    def __init__(self, session, **kwargs):
        self.stats = kwargs["stats"]

    async def run(self, max_decisions=None):
        return self.stats


class AbortedSession:
    abort_reason = "fatal control error INVALID_AUTHENTICATION"
    abort_code = ControlCode.INVALID_AUTHENTICATION

    def __init__(self, config, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


async def test_a_session_the_server_aborted_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setattr("bazaar_client.autonomous.BazaarSession", AbortedSession)
    monkeypatch.setattr("bazaar_client.autonomous.TradingLoop", NoopLoop)
    path = tmp_path / "e.jsonl"

    await run_trading(CONFIG, path, max_attempts=3)

    closed = [json.loads(line) for line in path.read_text().splitlines() if '"closed"' in line]
    assert closed[0]["category"] == "authentication"


async def test_an_abort_raised_by_the_loop_is_not_retried(monkeypatch, tmp_path):
    class RunChanged(NoopLoop):
        async def run(self, max_decisions=None):
            raise SessionAbortedError("run changed on reconnect")

    class Plain(AbortedSession):
        abort_reason = None

    monkeypatch.setattr("bazaar_client.autonomous.BazaarSession", Plain)
    monkeypatch.setattr("bazaar_client.autonomous.TradingLoop", RunChanged)
    path = tmp_path / "e.jsonl"

    await run_trading(CONFIG, path, max_attempts=3)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(r.get("detail") == "aborted: run changed on reconnect" for r in records)
    assert records[-1]["final_status"] == "disconnected"


# --- CLI entry points --------------------------------------------------------


async def test_trade_mode_fails_when_no_state_ever_arrived(monkeypatch):
    stats = TradingStats()

    async def fake_run(config, evidence, max_decisions):
        return stats

    monkeypatch.setattr("bazaar_client.autonomous.run_trading", fake_run)
    assert await cli.run_trade_mode(CONFIG) == 1
    stats.final_snapshot = f.make_snapshot()
    assert await cli.run_trade_mode(CONFIG) == 0


async def test_walkthrough_mode_reports_each_failed_check(monkeypatch, caplog):
    result = WalkthroughResult(checks=[Check("1", "ok", True), Check("2", "stock", False, "30 != 28")])

    async def fake_walkthrough(config, evidence):
        return result

    monkeypatch.setattr("bazaar_client.scripted_walkthrough.run_walkthrough", fake_walkthrough)
    with caplog.at_level(logging.INFO, logger="bazaar_client.cli"):
        assert await cli.run_walkthrough_mode(CONFIG) == 1
    assert "[2] stock: 30 != 28" in caplog.text and "1 of 2 checks passed" in caplog.text


class CheckSession:
    def __init__(self, ack_sequence):
        self.snapshot = f.make_snapshot()
        self.ack = f.make_readiness(run_id=self.snapshot.run_id, snapshot_sequence=ack_sequence)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def wait_for_snapshot(self, min_sequence=1, timeout=15.0):
        return self.snapshot

    async def wait_for_readiness(self, timeout=15.0):
        return self.ack


@pytest.mark.parametrize("ack_sequence, code", [(1, 0), (9, 1)])
async def test_check_mode_walks_the_status_ladder_and_verifies_readiness(monkeypatch, caplog,
                                                                        ack_sequence, code):
    monkeypatch.setattr("bazaar_client.cli.BazaarSession", lambda config: CheckSession(ack_sequence))
    with caplog.at_level(logging.INFO, logger="bazaar_client.cli"):
        assert await cli.run_check_mode(CONFIG) == code
    for status in ("starting", "connected", "authenticated", "synchronized"):
        assert f"status {status}" in caplog.text


def test_keyboard_interrupt_exits_with_130(monkeypatch):
    async def interrupted(config):
        raise KeyboardInterrupt

    monkeypatch.setattr("bazaar_client.cli.run_trade_mode", interrupted)
    assert cli.main(["--token", "t"]) == 130


def test_version_can_come_from_the_process_arguments(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["bazaar-client", "--version"])
    assert cli.main() == 0
    assert "bazaar-client" in capsys.readouterr().out


# --- strategy gates, diagnostics, build state, evidence files ---------------


@pytest.mark.parametrize("snapshot, reason", [
    (f.make_snapshot(me=f.make_station(failed_once=True)), "not considered: our station has failed"),
    (f.make_snapshot(tick=120), "not considered: run duration reached"),
])
def test_offers_the_policy_never_looked_at_say_why(snapshot, reason):
    offer = f.make_offer(offer_id="x", proposer_id="P02", recipient_id="P01",
                         give=Bundle(food=1), receive=Bundle.zero(), expires_tick=200)
    snapshot = replace(snapshot, offers=(offer,))
    strategy = get_strategy("reserve-trader")
    decision, _ = strategy.decide(snapshot, PolicyMemory())

    assert strategy.explain_passes(snapshot, decision) == {"x": reason}


def test_rate_limited_offers_are_not_considered():
    offer = f.make_offer(offer_id="x", proposer_id="P02", recipient_id="P01",
                         give=Bundle(food=1), receive=Bundle.zero(), expires_tick=50)
    snapshot = f.make_snapshot(offers=(offer,), tick=3)
    strategy = get_strategy("reserve-trader")
    memory = PolicyMemory()
    memory.block_until(9)
    decision, _ = strategy.decide(snapshot, memory)

    assert strategy.explain_passes(snapshot, decision)["x"].startswith("not considered: rate limited")


def test_a_generic_handshake_failure_and_an_odd_http_status_are_protocol_problems():
    assert diagnose(InvalidHandshake("garbled")).kind is FailureKind.PROTOCOL
    teapot = diagnose(InvalidStatus(Response(418, "teapot", Headers(), b"")))
    assert teapot.kind is FailureKind.PROTOCOL and "HTTP 418" in teapot.hint


@pytest.mark.parametrize("stdout, dirty", [(" M bazaar_client/app.py\n", True), ("", False)])
def test_uncommitted_changes_are_detected(monkeypatch, stdout, dirty):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, ""))
    assert working_tree_dirty() is dirty


def test_without_git_the_tree_state_is_unknown(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert working_tree_dirty() is None


def test_rotation_never_overwrites_an_earlier_saved_log(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text("first\n")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    first = rotate(path)
    path.write_text("second\n")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    second = rotate(path)

    assert first != second and second.name.endswith("-1.jsonl")
    assert (first.read_text(), second.read_text()) == ("first\n", "second\n")


def test_an_in_memory_log_reads_back_its_commands_and_knows_it_has_no_file():
    log = EvidenceLog()
    log.complete(log.start("tick-1", AcceptAction("o-1"), None))
    log.status_event("waiting")

    assert log.path is None
    assert log.read_back()[0]["action_kind"] == "accept"


# --- executor branches -------------------------------------------------------


async def test_a_command_with_no_observed_state_has_no_deadline_to_miss():
    session = FakeSession()
    session.latest_snapshot = None
    executor, _, evidence, _, _ = make_executor(session)

    await executor.execute(AcceptAction("o-1"), "r-1")

    assert evidence.records[0].deadline_missed is None
    assert executor.latency.deadline_checks == 0


async def test_a_blocked_offer_releases_its_hold_at_once():
    executor, _, _, commitments, _ = make_executor(FakeSession(error=CommandBlockedError("paused")))

    with pytest.raises(CommandBlockedError):
        await executor.execute(OfferAction("P02", Bundle(water=2), Bundle(food=2), 5), "r-1")

    assert commitments.inflight_total == Bundle.zero()
    assert executor.commitments is commitments


async def test_station_failed_on_an_accept_names_no_recipient_to_mark():
    refused = CommandOutcome("r-1", result=f.make_result(
        request_id="r-1", ok=False, code=ResultCode.STATION_FAILED))
    executor, _, evidence, _, counterparties = make_executor(FakeSession(outcome=refused))
    counterparties._stations["P02"] = __import__(
        "bazaar_client.world.counterparties", fromlist=["CounterpartyStats"]).CounterpartyStats("P02")

    await executor.execute(AcceptAction("o-1"), "r-1")

    assert evidence.records[0].result_code == "STATION_FAILED"
    assert [s.station_id for s in counterparties.stations()] == ["P02"]  # nobody marked failed
