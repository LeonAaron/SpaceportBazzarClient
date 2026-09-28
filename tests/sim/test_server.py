"""Our test server, driven by real clients over a real socket.

Every expected inventory here is worked out by hand from the rules in the
comments, so a failure means the server and the documented rules disagree.
Ticks are advanced by the test (tick_ms=0), so nothing depends on timing.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from bazaar_client.app import BazaarSession
from bazaar_client.codec.wire import decode_server_message, encode_client_message
from bazaar_client.config import SUBPROTOCOL, ClientConfig, Secret
from bazaar_client.diagnostics import FailureKind, diagnose
from bazaar_client.domain import mappers
from bazaar_client.domain.types import (
    Bundle,
    CommandResult,
    ControlCode,
    OfferStatus,
    Phase,
    ProtocolErrorEvent,
    Resource,
    ResultCode,
)
from bazaar_client.execution.actions import AcceptAction
from bazaar_sim.economy import DEFAULT_RULES
from bazaar_sim.server import BazaarServer, build_economy

RULES = replace(DEFAULT_RULES, duration_ticks=3, new_commands_per_station_per_tick=2)
RUN = "test-run"


@pytest.fixture
async def server(tmp_path):
    economy = build_economy(2, rules=RULES, starting_stock=10, run_id=RUN)
    server = BazaarServer(economy, tick_ms=0, start_when=0, production=lambda tick, i: 2,
                          report_path=tmp_path / "report.json")
    await server.start()
    yield server
    await server.stop()


def config(server: BazaarServer, station_id: str, token: str | None = None) -> ClientConfig:
    by_station = {sid: t for t, sid in server.tokens.items()}
    return ClientConfig(ws_url=server.url(), token=Secret(token or by_station[station_id]),
                        station_id=station_id, connect_timeout_s=5)


async def until(session: BazaarSession, predicate, timeout: float = 5.0):
    """The first state, from the newest held one on, that satisfies `predicate`."""
    snapshot = session.latest_snapshot
    while not predicate(snapshot):
        snapshot = await session.wait_for_snapshot(snapshot.snapshot_sequence + 1, timeout)
    return snapshot


async def start_running(server, *sessions):
    await server.start_run()
    for session in sessions:
        await until(session, lambda s: s.phase is Phase.RUNNING)


async def offer(session, recipient, give, receive, expires, request_id):
    message = mappers.build_offer(session.run_id, request_id, recipient, give, receive, expires)
    return await session.send_command(message, "offer", request_id)


async def accept(session, offer_id, request_id):
    return await session.send_command(
        mappers.build_accept(session.run_id, request_id, offer_id), "accept", request_id)


class RawClient:
    """A hand-rolled minimal client, so server checks do not rely on our session's guards."""

    def __init__(self, server: BazaarServer, station_id: str, ready: bool = True) -> None:
        self._url = server.url()
        self._token = config(server, station_id).token.reveal()
        self._ready = ready

    async def __aenter__(self) -> RawClient:
        self.ws = await websockets.connect(self._url, subprotocols=[SUBPROTOCOL], additional_headers={
            "Authorization": f"Bearer {self._token}"})
        first = await self.recv()
        if self._ready:
            await self.send(mappers.build_ready(RUN, True, first.snapshot_sequence))
            await self.recv()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.ws.close()

    async def send(self, message) -> None:
        await self.ws.send(encode_client_message(message))

    async def recv(self):
        return decode_server_message(await self.ws.recv())

    async def answer(self):
        """The next result or protocol error, skipping the states in between."""
        while True:
            event = await self.recv()
            if isinstance(event, (CommandResult, ProtocolErrorEvent)):
                return event


# --- a complete exchange between two clients -------------------------------


async def test_two_clients_complete_an_exchange_with_hand_checked_inventories(server):
    async with BazaarSession(config(server, "P01")) as p01, \
               BazaarSession(config(server, "P02")) as p02:
        first, _ = await p01.handshake()
        await p02.handshake()
        assert first.me.specialty is Resource.WATER and first.me.inventory == Bundle(10, 10, 10)
        await start_running(server, p01, p02)

        # P01 offers 4 water for 3 food. Posting reserves nothing.
        posted = await offer(p01, "P02", Bundle(water=4), Bundle(food=3), 2, "o-1")
        assert posted.ok and posted.result.object_id
        assert (await p01.wait_for_result_snapshot(posted.result)).me.inventory == Bundle(10, 10, 10)

        # P02 accepts: both bundles move at once.
        taken = await accept(p02, posted.result.object_id, "a-1")
        assert taken.ok and taken.result.transaction_id
        p02_after = await p02.wait_for_result_snapshot(taken.result)
        assert p02_after.me.inventory == Bundle(water=14, food=7, components=10)
        p01_after = await until(p01, lambda s: s.transactions)
        assert p01_after.me.inventory == Bundle(water=6, food=13, components=10)

        # One tick: +2 of the specialty, then 1 of each consumed.
        await server.advance()
        tick_1 = await until(p01, lambda s: s.tick == 1)
        assert tick_1.me.inventory == Bundle(water=6 + 2 - 1, food=13 - 1, components=10 - 1)
        assert tick_1.me.health == 100
        assert server.economy.stations["P02"].inventory == Bundle(14 - 1, 7 + 2 - 1, 10 - 1)


# --- the rules clients depend on -------------------------------------------


async def test_an_accept_the_proposer_can_no_longer_pay_moves_nothing(server):
    async with BazaarSession(config(server, "P01")) as p01, \
               BazaarSession(config(server, "P02")) as p02:
        await p01.handshake()
        await p02.handshake()
        await start_running(server, p01, p02)
        posted = await offer(p01, "P02", Bundle(water=8), Bundle(food=1), 2, "o-1")
        second = await offer(p01, "P02", Bundle(water=8), Bundle(food=1), 2, "o-2")
        assert posted.ok and second.ok  # 8 + 8 promised out of 10: there is no escrow

        first_take = await accept(p02, posted.result.object_id, "a-1")
        refused = await accept(p02, second.result.object_id, "a-2")

    assert first_take.ok
    assert refused.result.code is ResultCode.INSUFFICIENT_RESOURCES
    assert server.economy.stations["P01"].inventory == Bundle(water=2, food=11, components=10)


async def test_the_per_tick_command_limit_is_enforced_with_a_retry_tick(server):
    async with RawClient(server, "P01") as p01:
        await server.start_run()
        answers = []
        for n in range(RULES.new_commands_per_station_per_tick + 1):
            await p01.send(mappers.build_advertise(RUN, f"ad-{n}", [Resource.WATER], [Resource.FOOD], 2))
            answers.append(await p01.answer())

    assert [a.code for a in answers] == [ResultCode.OK, ResultCode.OK, ResultCode.RATE_LIMITED]
    assert answers[-1].retry_after_tick == 1


async def test_an_exact_retry_returns_the_stored_result_and_a_changed_one_conflicts(server):
    async with RawClient(server, "P01") as p01:
        await server.start_run()
        command = mappers.build_offer(RUN, "o-1", "P02", Bundle(water=1), Bundle(food=1), 2)
        await p01.send(command)
        original = await p01.answer()
        await p01.send(command)
        replayed = await p01.answer()
        await p01.send(mappers.build_offer(RUN, "o-1", "P02", Bundle(water=2), Bundle(food=1), 2))
        conflict = await p01.answer()

    assert original.ok and replayed == original
    assert len([o for o in server.economy.offers if o.proposer_id == "P01"]) == 1
    assert conflict.code is ResultCode.REQUEST_ID_CONFLICT


async def test_commands_before_readiness_are_refused_but_the_session_stays_open(server):
    async with RawClient(server, "P01", ready=False) as p01:
        await p01.send(mappers.build_accept(RUN, "a-1", "offer-1"))
        refusal = await p01.answer()
        await p01.send(mappers.build_sync(RUN))
        still_served = await p01.recv()

    assert isinstance(refusal, ProtocolErrorEvent)
    assert refusal.code is ControlCode.BAD_MESSAGE and not refusal.close_session
    assert refusal.request_id == "a-1"
    assert still_served.snapshot_sequence == 2


async def test_a_wrong_run_id_closes_the_session(server):
    async with RawClient(server, "P01") as p01:
        await p01.send(mappers.build_sync("some-other-run"))
        refusal = await p01.answer()

    assert refusal.code is ControlCode.RUN_MISMATCH and refusal.close_session


async def test_an_unsupported_protocol_version_closes_the_session(server):
    async with RawClient(server, "P01") as p01:
        message = mappers.build_sync(RUN)
        message.sync.protocol_version = "1.0"
        await p01.send(message)
        refusal = await p01.answer()

    assert refusal.code is ControlCode.UNSUPPORTED_VERSION and refusal.close_session


async def test_an_offer_expires_at_its_deadline_and_can_no_longer_be_accepted(server):
    async with RawClient(server, "P01") as p01:
        await server.start_run()
        await p01.send(mappers.build_offer(RUN, "o-1", "P02", Bundle(water=1), Bundle(food=1), 1))
        posted = await p01.answer()
        await server.advance()  # tick 1: an offer expiring at tick 1 is unusable from tick 1

    offers = {o.offer_id: o for o in server.economy.offers}
    assert offers[posted.object_id].status is OfferStatus.EXPIRED
    assert offers[posted.object_id].closed_tick == 1
    assert server.economy.execute("P02", AcceptAction(posted.object_id)).code is ResultCode.NOT_OPEN


async def test_the_run_finishes_with_an_outcome_and_a_scored_report(server, tmp_path):
    async with BazaarSession(config(server, "P01")) as p01:
        await p01.handshake()
        await start_running(server, p01)
        for _ in range(RULES.duration_ticks):
            await server.advance()
        final = await until(p01, lambda s: s.phase is Phase.FINISHED)

    assert final.outcome is not None and final.outcome.collective_success is True
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["phase"] == "FINISHED"
    # 2 planets x 3 ticks, nobody short: 10 of each covers three ticks of upkeep.
    assert report["score"] == {
        "planets": 2, "collective_success": True, "survivors": 2,
        "world_alive_ticks": 6, "shortage_ticks": 0, "unmet_units": 0,
        "subject_survived": None, "subject_health": None,
    }


async def test_a_new_connection_for_the_same_planet_fences_the_old_one(server):
    async with RawClient(server, "P01") as old:
        async with RawClient(server, "P01"):
            fenced = await old.answer()

    assert fenced.code is ControlCode.SESSION_FENCED and fenced.close_session


# --- handshake failures are diagnosed by category --------------------------


async def test_a_wrong_token_is_refused_with_401_and_diagnosed_as_authentication(server):
    with pytest.raises(InvalidStatus) as refused:
        async with BazaarSession(config(server, "P01", token="not-a-real-token")):
            pass

    assert refused.value.response.status_code == 401
    assert diagnose(refused.value).kind is FailureKind.AUTHENTICATION


async def test_a_missing_subprotocol_is_refused_with_400_and_diagnosed_as_protocol(server):
    with pytest.raises(InvalidStatus) as refused:
        async with websockets.connect(server.url()):
            pass

    assert refused.value.response.status_code == 400
    assert diagnose(refused.value).kind is FailureKind.PROTOCOL


async def test_open_auth_seats_planets_in_connection_order():
    server = BazaarServer(build_economy(2, rules=RULES, run_id="open"),
                          open_auth=True, tick_ms=0, start_when=0)
    await server.start()
    try:
        def anyone():
            return ClientConfig(ws_url=server.url(), token=Secret("ignored"), station_id="?")

        async with BazaarSession(anyone()) as first, BazaarSession(anyone()) as second:
            a, _ = await first.handshake()
            b, _ = await second.handshake()
        assert (a.self_station_id, b.self_station_id) == ("P01", "P02")
    finally:
        await server.stop()
