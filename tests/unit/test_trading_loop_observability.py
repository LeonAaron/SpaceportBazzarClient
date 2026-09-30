"""What the trading loop records about itself: links, timings, waits and stalls."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from bazaar_client.autonomous import TradingLoop, TradingStats, run_trading
from bazaar_client.cli import main
from bazaar_client.config import ClientConfig, Secret
from bazaar_client.domain.types import Bundle, Phase
from bazaar_client.execution.evidence import EvidenceLog
from bazaar_client.policy.decide import Decision
from bazaar_client.status import ClientStatus, StatusTracker
from bazaar_client.strategy import get_strategy
from tests.fixtures import factories as f
from tests.unit.test_session_routing import (
    FakeConnection,
    complete_handshake,
    push,
    start_session,
)


def gift(offer_id="gift-1"):
    return f.make_offer(offer_id=offer_id, proposer_id="P02", recipient_id="P01",
                        give=Bundle(food=2), receive=Bundle.zero(), expires_tick=50)


async def test_a_command_carries_the_id_of_the_decision_that_caused_it(tmp_path):
    connection = FakeConnection()
    session = await start_session(connection)
    evidence = EvidenceLog(tmp_path / "evidence.jsonl")
    try:
        state = await complete_handshake(connection, session, offers=(gift(),))
        loop = TradingLoop(session, evidence=evidence)
        pending = asyncio.create_task(loop.step(state))
        await asyncio.sleep(0)
        request_id = connection.sent[-1].accept.request_id
        result = f.make_result(request_id=request_id, processed_tick=0, object_id="gift-1")
        await push(connection, replace(state, snapshot_sequence=2, request_results=(result,)), session)
        await pending
    finally:
        await session.stop()

    decision, command = evidence.read_back()
    assert command["decision_id"] == decision["decision_id"] == "d1"
    assert command["request_id"] == request_id and command["result_code"] == "OK"
    assert command["response_ms"] >= 0 and command["confirm_ms"] >= 0
    assert command["deadline_missed"] is False
    assert decision["verdict"] == "act" and decision["timing"]["decide_ms"] >= 0
    assert loop.latency.summary()["response"].count == 1


async def test_a_command_processed_in_a_later_tick_missed_its_deadline(tmp_path):
    connection = FakeConnection()
    session = await start_session(connection)
    evidence = EvidenceLog()
    try:
        state = await complete_handshake(connection, session, offers=(gift(),))
        loop = TradingLoop(session, evidence=evidence)
        pending = asyncio.create_task(loop.step(state))
        await asyncio.sleep(0)
        request_id = connection.sent[-1].accept.request_id
        late = f.make_result(request_id=request_id, processed_tick=1, object_id="gift-1")
        await push(connection, replace(state, tick=1, snapshot_sequence=2, request_results=(late,)),
                   session)
        await pending
    finally:
        await session.stop()

    assert evidence.records[0].deadline_missed is True
    assert loop.latency.missed_deadlines == 1


async def test_a_decision_to_wait_is_recorded_with_its_reason():
    connection = FakeConnection()
    session = await start_session(connection)
    evidence = EvidenceLog()
    try:
        state = await complete_handshake(connection, session, offers=(gift(),))
        loop = TradingLoop(session, evidence=evidence, strategy=get_strategy("passive"))
        await loop.step(state)
    finally:
        await session.stop()

    record = evidence.decisions[0]
    assert record["verdict"] == "wait"
    assert record["wait_reason"] == "nothing met the strategy's criteria to act"
    assert record["passed_offers"] == {"gift-1": "passive strategy never accepts"}
    assert record["strategy"] == "passive"
    assert record["timing"]["queue_ms"] is not None
    assert len(connection.sent) == 1  # only the readiness declaration


def test_ticks_that_pass_without_any_decision_are_flagged():
    stats, evidence = TradingStats(), EvidenceLog()
    loop = TradingLoop(object(), stats=stats, evidence=evidence)
    for tick in (1, 2, 6):
        snapshot = f.make_snapshot(tick=tick)
        loop._log_decision(snapshot, Decision(), {})

    assert [d.get("ticks_skipped") for d in evidence.decisions] == [None, None, 3]


@dataclass(frozen=True)
class SlowStrategy:
    """Blocks for a while, as a heavy computation would."""

    seconds: float
    name: str = "slow"
    description: str = "sleeps, then does nothing"

    def decide(self, observation, memory, commitments=None, *, command_budget=None):
        time.sleep(self.seconds)
        return Decision(), memory

    def explain_passes(self, observation, decision):
        return {}


@pytest.mark.parametrize("in_thread, expected", [(True, 1), (False, 0)])
async def test_states_keep_arriving_while_a_slow_strategy_thinks(in_thread, expected):
    """With the strategy on a worker thread, the socket reader keeps running."""
    connection = FakeConnection()
    session = await start_session(connection)
    evidence = EvidenceLog()
    try:
        state = await complete_handshake(connection, session)
        loop = TradingLoop(session, evidence=evidence, strategy=SlowStrategy(0.3),
                           decide_in_thread=in_thread)
        thinking = asyncio.create_task(loop.step(state))
        await asyncio.sleep(0.05)
        await push(connection, replace(state, snapshot_sequence=2), session)
        await thinking
    finally:
        await session.stop()

    assert evidence.decisions[0]["states_during_decision"] == expected
    assert evidence.decisions[0]["timing"]["decide_ms"] >= 300


class FakeClock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_a_running_game_that_goes_silent_is_marked_stale_and_recovers(tmp_path):
    connection = FakeConnection()
    session = await start_session(connection)
    clock = FakeClock()
    evidence = EvidenceLog(tmp_path / "evidence.jsonl")
    status = StatusTracker(clock=clock, on_change=lambda old, new, why: evidence.status_event(
        new.value, previous=old.value, detail=why))
    rules = f.make_rules(tick_duration_ms=100)  # stale after 2s of silence, polled every 1s
    task = None
    try:
        state = await complete_handshake(connection, session, rules=rules)
        loop = TradingLoop(session, evidence=evidence, status=status,
                           strategy=get_strategy("passive"))
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.1)
        assert status.current is ClientStatus.PARTICIPATING

        clock.now += 5.0
        await asyncio.sleep(1.2)
        assert status.current is ClientStatus.STALE

        await push(connection, replace(state, tick=1, snapshot_sequence=2), session)
        assert status.current is ClientStatus.PARTICIPATING
        await push(connection, replace(state, tick=2, snapshot_sequence=3, phase=Phase.FINISHED), session)
        await asyncio.wait_for(task, 2)
    finally:
        if task is not None and not task.done():
            task.cancel()
        await session.stop()

    changes = [(r.get("previous"), r["status"]) for r in evidence.read_back() if r.get("kind") == "status"]
    assert ("participating", "stale") in changes and ("stale", "participating") in changes
    assert changes[-1] == ("participating", "finished")


# --- failures that retrying cannot fix -------------------------------------


def http(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "Unauthorized", Headers(), b""))


class RefusingSession:
    def __init__(self, config, **kwargs):
        pass

    async def __aenter__(self):
        raise http(401)

    async def __aexit__(self, *exc):
        return None


async def test_a_rejected_token_is_not_retried_and_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr("bazaar_client.autonomous.BazaarSession", RefusingSession)
    path = tmp_path / "evidence.jsonl"
    config = ClientConfig(ws_url="ws://test/ws", token=Secret("bad"), station_id="P01")
    slept = []

    async def sleep(delay):
        slept.append(delay)

    with pytest.raises(InvalidStatus):
        await run_trading(config, path, max_attempts=5, sleep=sleep)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    failed = next(r for r in records if r.get("event") == "failed")
    assert failed["category"] == "authentication"
    assert records[0]["kind"] == "run_start" and records[-1]["kind"] == "run_end"
    assert records[-1]["final_status"] == "disconnected"
    assert slept == []


def test_the_exit_code_names_the_failure_category(monkeypatch):
    monkeypatch.setattr("bazaar_client.app.BazaarSession", RefusingSession)

    assert main(["--token", "bad", "--mode", "check"]) == 3
