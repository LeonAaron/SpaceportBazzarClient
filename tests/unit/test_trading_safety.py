"""Regression tests for decisions made while results and snapshots are in flight."""

import asyncio
from dataclasses import replace

import pytest

from bazaar_client.app import CommandBlockedError
from bazaar_client.autonomous import TradingLoop
from bazaar_client.connection.throttle import CommandThrottle
from bazaar_client.domain import mappers
from bazaar_client.domain.types import Bundle, Phase, Resource, ResultCode
from bazaar_client.execution.actions import AcceptAction, OfferAction
from bazaar_client.execution.evidence import EvidenceLog
from bazaar_client.execution.executor import Executor
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.world.commitments import CommitmentTracker
from tests.fixtures import factories as f
from tests.unit.test_session_routing import (
    FakeConnection, complete_handshake, push, start_session,
)


def advertisement(station_id="P02", selling=Resource.FOOD):
    return f.make_advertisement(
        station_id=station_id, selling=frozenset({selling}),
        seeking=frozenset({Resource.WATER}), expires_tick=120,
    )


@pytest.mark.parametrize("phase", [Phase.READY, Phase.PAUSED, Phase.FINISHED, Phase.ABORTED])
async def test_phase_change_blocks_transmission(phase):
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        await push(connection, f.make_snapshot(snapshot_sequence=2, phase=phase), session)
        before = len(connection.sent)
        with pytest.raises(CommandBlockedError):
            await session.send_command(
                mappers.build_accept(session.run_id, "blocked", "offer-1"), "accept", "blocked"
            )
        assert len(connection.sent) == before
    finally:
        await session.stop()


async def test_not_ready_and_recovered_failure_cannot_send():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await push(connection, f.make_snapshot(), session)
        message = mappers.build_accept(session.run_id, "blocked", "offer-1")
        with pytest.raises(CommandBlockedError):
            await session.send_command(message, "accept", "blocked")
        await push(connection, f.make_readiness(), session)
        await push(connection, f.make_snapshot(
            snapshot_sequence=2, me=f.make_station(failed_once=True, health=25)
        ), session)
        with pytest.raises(CommandBlockedError):
            await session.send_command(message, "accept", "blocked")
        assert len(connection.sent) == 1  # only ready
    finally:
        await session.stop()


async def test_budget_survives_new_snapshots_and_exact_retries_do_not_spend_it():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        state = await complete_handshake(connection, session, rules=f.make_rules(
            new_commands_per_station_per_tick=1
        ))
        message = mappers.build_accept(session.run_id, "first", "offer-1")
        for _ in range(2):
            pending = asyncio.create_task(session.send_command(message, "accept", "first"))
            await asyncio.sleep(0)
            await push(connection, f.make_result(request_id="first"), session)
            assert (await pending).ok
        await push(connection, replace(state, snapshot_sequence=2), session)
        before = len(connection.sent)
        with pytest.raises(CommandBlockedError):
            await session.send_command(
                mappers.build_accept(session.run_id, "second", "offer-2"), "accept", "second"
            )
        assert len(connection.sent) == before
        await push(connection, replace(state, snapshot_sequence=3, tick=1), session)
        assert session.remaining_command_budget == 1
    finally:
        await session.stop()


async def test_stale_running_snapshot_cannot_override_a_pause():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        state = await complete_handshake(connection, session)
        await push(connection, replace(state, snapshot_sequence=3, phase=Phase.PAUSED), session)
        await push(connection, replace(state, snapshot_sequence=2), session)
        assert session.remaining_command_budget == 0
        assert session.lifecycle.phase is Phase.PAUSED
    finally:
        await session.stop()


@pytest.mark.parametrize("ok", [True, False])
async def test_evidence_waits_for_matching_result_even_at_unchanged_world_version(ok):
    connection = FakeConnection()
    session = await start_session(connection)
    pending = None
    try:
        state = await complete_handshake(connection, session)
        evidence = EvidenceLog()
        commitments = CommitmentTracker()
        executor = Executor(session, evidence, commitments)
        action = OfferAction("P02", Bundle(water=2), Bundle(food=1), 5)
        result = f.make_result(
            request_id="offer", ok=ok, processed_version=state.world_version,
            code=ResultCode.OK if ok else ResultCode.INSUFFICIENT_RESOURCES,
        )
        pending = asyncio.create_task(executor.execute(action, "offer"))
        await asyncio.sleep(0)
        await push(connection, result, session)
        await push(connection, replace(state, snapshot_sequence=2), session)
        assert not pending.done()
        assert evidence.records[0].confirmed_snapshot_sequence is None
        assert commitments.inflight_total == (action.give if ok else Bundle.zero())
        confirmed = replace(state, snapshot_sequence=3, request_results=(result,))
        await push(connection, confirmed, session)
        assert (await pending).ok is ok
        assert evidence.records[0].confirmed_snapshot_sequence == 3
        assert commitments.inflight_total.is_zero()
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        await session.stop()


async def test_snapshot_recovers_a_lost_result_message():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        state = await complete_handshake(connection, session)
        result = f.make_result(request_id="accept")
        pending = asyncio.create_task(session.send_command(
            mappers.build_accept(session.run_id, "accept", "offer-1"), "accept", "accept"
        ))
        await asyncio.sleep(0)
        confirmed = replace(state, snapshot_sequence=2, request_results=(result,))
        await push(connection, confirmed, session)
        assert (await pending).result == result
        assert await session.wait_for_result_snapshot(result) == confirmed
    finally:
        await session.stop()


async def test_timeout_keeps_uncertain_stock_and_does_not_claim_confirmation():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        evidence = EvidenceLog()
        commitments = CommitmentTracker()
        executor = Executor(session, evidence, commitments)
        with pytest.raises(asyncio.TimeoutError):
            await executor.execute(
                OfferAction("P02", Bundle(water=2), Bundle(food=1), 5), "lost", timeout=0.01
            )
        assert commitments.inflight_total == Bundle(water=2)
        assert evidence.records[0].confirmed_snapshot_sequence is None
        assert not session._command_waiters
    finally:
        await session.stop()


async def test_loop_replans_after_one_action_and_honours_new_pause():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        state = await complete_handshake(connection, session, offers=(f.make_offer(
            proposer_id="P02", recipient_id="P01", give=Bundle(food=2), receive=Bundle.zero()
        ),))
        loop = TradingLoop(session)
        pending = asyncio.create_task(loop.step(state))
        await asyncio.sleep(0)
        sent = connection.sent[-1]
        assert sent.WhichOneof("message") == "accept"
        result = f.make_result(request_id=sent.accept.request_id)
        paused = replace(state, snapshot_sequence=2, phase=Phase.PAUSED, request_results=(result,))
        await push(connection, paused, session)
        decision = await pending
        assert len(decision.actions) == 1
        assert (await loop.step(paused)).actions == []
        assert len(connection.sent) == 2  # ready, then accept
    finally:
        await session.stop()


def test_acceptances_and_new_offers_share_one_spending_balance():
    incoming = f.make_offer(
        proposer_id="P02", recipient_id="P01", give=Bundle(food=10), receive=Bundle(water=7)
    )
    state = f.make_snapshot(me=f.make_station(inventory=Bundle(10, 0, 0)), offers=(incoming,),
                            advertisements=(advertisement(),))
    decision, _ = decide(state, PolicyMemory())
    assert AcceptAction(incoming.offer_id) in decision.actions
    promised = Bundle.zero()
    for action in decision.actions:
        if isinstance(action, AcceptAction):
            promised += incoming.receive
        if isinstance(action, OfferAction):
            promised += action.give
    assert promised.water <= state.me.inventory.water - decision.reserve.water
    assert not any(isinstance(a, OfferAction) and a.give.food for a in decision.actions)


def test_gifts_use_only_surplus_left_after_new_offers_and_obey_offer_limit():
    memory = PolicyMemory()
    ads = (advertisement(), advertisement("P03", Resource.COMPONENTS))
    for tick in range(5):
        memory.observe(f.make_snapshot(tick=tick, advertisements=ads))
    state = f.make_snapshot(tick=5, me=f.make_station(inventory=Bundle(5, 3, 3)),
                            advertisements=ads, rules=f.make_rules(max_open_outgoing_offers=1))
    decision, _ = decide(state, memory)
    offers = [a for a in decision.actions if isinstance(a, OfferAction)]
    assert len(offers) == 1
    assert offers[0].give.water <= 2


@pytest.mark.parametrize("tick", [119, 120])
def test_all_publication_deadlines_respect_run_end(tick):
    state = f.make_snapshot(tick=tick, me=f.make_station(inventory=Bundle(20, 0, 0)),
                            advertisements=(advertisement(),))
    decision, _ = decide(state, PolicyMemory())
    if tick == 120:
        assert not decision.actions
    else:
        deadlines = [a.expires_tick for a in decision.actions if hasattr(a, "expires_tick")]
        assert deadlines and all(d == 120 for d in deadlines)


async def test_reconnection_keeps_tick_budget_and_uses_fresh_request_ids():
    from bazaar_client.app import BazaarSession
    from tests.unit.test_session_routing import make_config
    throttle = CommandThrottle(1)
    ids = []
    for attempt in range(2):
        connection = FakeConnection()
        session = BazaarSession(make_config(), connection, throttle=throttle)
        await session.start()
        try:
            await complete_handshake(connection, session, rules=f.make_rules(
                new_commands_per_station_per_tick=1
            ))
            ids.append(session.request_ids.next("offer"))
            if attempt == 0:
                with pytest.raises(asyncio.TimeoutError):
                    await session.send_command(
                        mappers.build_accept(session.run_id, ids[-1], "offer-1"),
                        "accept", ids[-1], timeout=0.01,
                    )
            assert session.remaining_command_budget == 0
        finally:
            await session.stop()
    assert ids[0] != ids[1]


async def test_loop_uses_newest_balance_when_an_older_snapshot_was_queued():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        old = await complete_handshake(connection, session)
        latest = replace(old, snapshot_sequence=2, me=f.make_station(inventory=Bundle.zero()))
        await push(connection, latest, session)
        # Avoid sending: this test isolates selection of the observation.
        session._throttle.block_until(1)
        decision = await TradingLoop(session).step(old)
        assert decision.available.is_zero()
        assert not decision.actions
    finally:
        await session.stop()


async def test_reconnect_to_a_different_run_requires_fresh_memory():
    from bazaar_client.app import SessionAbortedError
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        loop = TradingLoop(session, memory=PolicyMemory(run_id="previous-run"))
        with pytest.raises(SessionAbortedError, match="run changed"):
            await loop.run()
        assert len(connection.sent) == 1
    finally:
        await session.stop()


async def test_rate_limit_blocks_sending_until_retry_tick():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        state = await complete_handshake(connection, session)
        await push(connection, f.make_result(
            request_id="earlier", ok=False, code=ResultCode.RATE_LIMITED, retry_after_tick=3
        ), session)
        for tick in (0, 1, 2):
            await push(connection, replace(state, snapshot_sequence=tick + 2, tick=tick), session)
            with pytest.raises(CommandBlockedError):
                await session.send_command(
                    mappers.build_accept(session.run_id, "new", "offer-1"), "accept", "new"
                )
        assert len(connection.sent) == 1
        await push(connection, replace(state, snapshot_sequence=5, tick=3), session)
        assert session.remaining_command_budget > 0
    finally:
        await session.stop()


async def test_full_loop_waits_for_next_tick_then_accepts_and_stops_at_finish():
    connection = FakeConnection()
    session = await start_session(connection)
    task = None
    try:
        state = await complete_handshake(connection, session, rules=f.make_rules(
            new_commands_per_station_per_tick=1
        ))
        loop = TradingLoop(session)
        task = asyncio.create_task(loop.run())

        async def wait_for_commands(count):
            async with asyncio.timeout(1):
                while len(connection.sent) < count + 1:  # plus readiness
                    await asyncio.sleep(0)

        await wait_for_commands(1)
        ad_command = connection.sent[-1].advertise
        result = f.make_result(request_id=ad_command.request_id)
        ad = f.make_advertisement(
            selling=frozenset(Resource), seeking=frozenset(), expires_tick=8
        )
        state = replace(state, snapshot_sequence=2, advertisements=(ad,), request_results=(result,))
        await push(connection, state, session)
        assert len(connection.sent) == 2
        assert not task.done()

        gift = f.make_offer(proposer_id="P02", recipient_id="P01",
                            give=Bundle(food=2), receive=Bundle.zero())
        state = replace(state, snapshot_sequence=3, tick=1, offers=(gift,))
        await push(connection, state, session)
        await wait_for_commands(2)
        assert connection.sent[-1].WhichOneof("message") == "accept"
        result = f.make_result(request_id=connection.sent[-1].accept.request_id)
        final = replace(state, snapshot_sequence=4, phase=Phase.FINISHED, request_results=(result,))
        await push(connection, final, session)
        stats = await asyncio.wait_for(task, 1)
        assert stats.commands_sent == stats.commands_ok == 2
        assert stats.accepts == stats.advertisements == 1
        assert stats.final_snapshot == final
    finally:
        if task is not None and not task.done():
            task.cancel()
        await session.stop()
