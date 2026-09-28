"""Honouring close_session, and recovering from a dropped connection.

A run lasts many ticks and upkeep is consumed throughout, so losing the
connection has to be survivable rather than final.
"""

from __future__ import annotations

import asyncio

import pytest

from bazaar_client.app import BazaarSession
from bazaar_client.autonomous import backoff_delay, run_trading
from bazaar_client.config import ClientConfig, Secret
from bazaar_client.connection.ws_client import ConnectionClosedSentinel
from bazaar_client.domain.types import ControlCode, Phase
from tests.fixtures import factories
from tests.unit.test_session_routing import FakeConnection, push


def make_config(**overrides) -> ClientConfig:
    defaults = dict(
        ws_url="ws://test/ws", token=Secret("t"), station_id="P01",
        reconnect_max_backoff_s=5.0,
    )
    return ClientConfig(**{**defaults, **overrides})


# --- close_session is actually honoured ----------------------------------


async def test_close_session_closes_the_connection():
    """The server has discarded this session; reading on would act on a dead one."""
    connection = FakeConnection()
    session = BazaarSession(make_config(), connection=connection)
    await session.start()
    try:
        snapshot = factories.make_snapshot()
        await push(connection, snapshot, session)
        await push(
            connection,
            factories.make_readiness(run_id=snapshot.run_id, snapshot_sequence=1),
            session,
        )

        await push(
            connection,
            factories.make_protocol_error(
                code=ControlCode.BAD_MESSAGE, close_session=True
            ),
            session,
        )

        assert connection.closed
        assert session.reconnect_wanted
    finally:
        await session.stop()


async def test_a_non_closing_error_leaves_the_connection_open():
    """README step 9: capacity exceeded, close_session false, keep the connection."""
    connection = FakeConnection()
    session = BazaarSession(make_config(), connection=connection)
    await session.start()
    try:
        snapshot = factories.make_snapshot()
        await push(connection, snapshot, session)
        await push(
            connection,
            factories.make_readiness(run_id=snapshot.run_id, snapshot_sequence=1),
            session,
        )

        await push(
            connection,
            factories.make_protocol_error(
                code=ControlCode.REQUEST_CAPACITY_EXCEEDED, close_session=False
            ),
            session,
        )

        assert not connection.closed
        assert not session.reconnect_wanted
    finally:
        await session.stop()


async def test_a_fatal_control_error_does_not_ask_for_a_reconnect():
    """Retrying with the same token or version cannot succeed."""
    connection = FakeConnection()
    session = BazaarSession(make_config(), connection=connection)
    await session.start()
    try:
        snapshot = factories.make_snapshot()
        await push(connection, snapshot, session)
        await push(
            connection,
            factories.make_readiness(run_id=snapshot.run_id, snapshot_sequence=1),
            session,
        )

        await push(
            connection,
            factories.make_protocol_error(code=ControlCode.INVALID_AUTHENTICATION),
            session,
        )

        assert session.abort_reason is not None
        assert not session.reconnect_wanted
    finally:
        await session.stop()


# --- backoff --------------------------------------------------------------


def test_backoff_grows_and_is_capped():
    cap = 10.0
    delays = [backoff_delay(attempt, cap) for attempt in range(1, 9)]

    assert delays[0] < delays[3]
    assert all(d <= cap * 1.2 for d in delays)


def test_backoff_is_jittered_so_clients_do_not_retry_in_lockstep():
    samples = {round(backoff_delay(4, 30.0), 6) for _ in range(20)}

    assert len(samples) > 1


# --- the reconnect supervisor --------------------------------------------


class ScriptedSession:
    """Stands in for a session whose behaviour each attempt is scripted."""

    attempts: list = []
    script: list = []

    def __init__(self, config, **kwargs):
        self.config = config
        self.behaviour = self.script[len(self.attempts)] if len(self.attempts) < len(self.script) else "ok"
        self.attempts.append(self.behaviour)
        self.abort_reason = None
        self.reconnect_wanted = False

    async def __aenter__(self):
        if self.behaviour == "refuse":
            raise OSError("connection refused")
        return self

    async def __aexit__(self, *exc):
        return None


def install(monkeypatch, script, run_behaviour):
    ScriptedSession.attempts = []
    ScriptedSession.script = script
    monkeypatch.setattr("bazaar_client.autonomous.BazaarSession", ScriptedSession)

    class FakeLoop:
        def __init__(self, session, **kwargs):
            self.session = session
            self.stats = kwargs["stats"]

        async def run(self, max_decisions=None):
            return run_behaviour(self)

    monkeypatch.setattr("bazaar_client.autonomous.TradingLoop", FakeLoop)
    return ScriptedSession


async def test_a_dropped_connection_is_retried(monkeypatch):
    slept = []

    def behaviour(loop):
        loop.stats.decisions += 1
        loop.stats.final_snapshot = factories.make_snapshot(phase=Phase.RUNNING)

    install(monkeypatch, ["refuse", "refuse", "ok"], behaviour)

    stats = await run_trading(
        make_config(), max_decisions=1, max_attempts=5, sleep=lambda d: _record(slept, d)
    )

    assert ScriptedSession.attempts == ["refuse", "refuse", "ok"]
    assert stats.decisions == 1
    assert len(slept) == 2  # slept only after the two failures


async def _record(bucket, delay):
    bucket.append(delay)


async def test_a_finished_run_is_not_reconnected(monkeypatch):
    def behaviour(loop):
        loop.stats.final_snapshot = factories.make_snapshot(phase=Phase.FINISHED)

    install(monkeypatch, ["ok", "ok"], behaviour)

    await run_trading(make_config(), max_attempts=4, sleep=lambda d: _record([], d))

    assert ScriptedSession.attempts == ["ok"]


async def test_reconnect_attempts_are_bounded(monkeypatch):
    def behaviour(loop):
        return None

    install(monkeypatch, ["refuse"] * 10, behaviour)

    await run_trading(make_config(), max_attempts=3, sleep=lambda d: _record([], d))

    assert len(ScriptedSession.attempts) == 3


# --- joining a run that has not started yet ----------------------------------


async def test_a_quiet_server_does_not_end_the_session(monkeypatch):
    """Before a run starts the server can go quiet for minutes. Run 2's P01
    dropped and reconnected ten times at tick 0 because silence was treated as
    a dead connection."""
    from bazaar_client.autonomous import TradingLoop

    connection = FakeConnection()
    session = BazaarSession(make_config(), connection=connection)
    await session.start()
    real_wait = session.wait_for_snapshot
    monkeypatch.setattr(
        session, "wait_for_snapshot",
        lambda min_sequence=1, timeout=15.0: real_wait(min_sequence, timeout=0.02),
    )
    task = None
    try:
        lobby = factories.make_snapshot(phase=Phase.READY)
        await push(connection, lobby, session)
        await push(connection, factories.make_readiness(run_id=lobby.run_id, snapshot_sequence=1), session)
        task = asyncio.create_task(TradingLoop(session).run())

        await asyncio.sleep(0.2)  # ten times the wait timeout, with no new state
        assert not task.done()
        assert not connection.closed

        finished = factories.make_snapshot(snapshot_sequence=2, phase=Phase.FINISHED)
        await push(connection, finished, session)
        stats = await asyncio.wait_for(task, 1)
        assert stats.final_snapshot.phase is Phase.FINISHED
    finally:
        if task is not None and not task.done():
            task.cancel()
        await session.stop()


async def test_a_connection_that_really_closes_still_ends_the_session():
    from bazaar_client.autonomous import TradingLoop

    connection = FakeConnection()
    session = BazaarSession(make_config(), connection=connection)
    await session.start()
    try:
        snapshot = factories.make_snapshot(phase=Phase.READY)
        await push(connection, snapshot, session)
        await push(connection, factories.make_readiness(run_id=snapshot.run_id, snapshot_sequence=1), session)
        task = asyncio.create_task(TradingLoop(session).run())
        await asyncio.sleep(0)

        await push(connection, ConnectionClosedSentinel(), session)

        await asyncio.wait_for(task, 1)
    finally:
        await session.stop()


async def test_after_a_working_session_a_drop_is_retried_quickly(monkeypatch):
    """Backoff is for a server that keeps refusing us, not for one good session
    after another; otherwise a mid-run drop could cost us 30 seconds of ticks."""
    slept = []

    def behaviour(loop):
        loop.stats.decisions += 1
        loop.stats.final_snapshot = factories.make_snapshot(phase=Phase.RUNNING)

    install(monkeypatch, ["ok"] * 6, behaviour)

    await run_trading(
        make_config(reconnect_max_backoff_s=30.0), max_decisions=5,
        sleep=lambda d: _record(slept, d),
    )

    assert len(slept) == 4
    assert max(slept) <= 1.2  # first-attempt delay every time, never growing
