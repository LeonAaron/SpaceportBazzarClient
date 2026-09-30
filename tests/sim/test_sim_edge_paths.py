"""Edge and failure paths of the server, codec, engine, benchmark and orchestrator."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
import websockets
from websockets.exceptions import ConnectionClosedOK, InvalidStatus

import bazaar_pb2
from bazaar_client.app import BazaarSession
from bazaar_client.config import ClientConfig, Secret
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
from bazaar_client.execution.actions import (
    AcceptAction,
    AdvertiseAction,
    OfferAction,
    SyncAction,
    WithdrawAction,
)
from bazaar_sim import benchmark, codec, orchestrate, server as server_module
from bazaar_sim.economy import DEFAULT_RULES, SimStation, SimulatedEconomy
from bazaar_sim.opponents import OurPolicy, Passive
from bazaar_sim.server import BazaarServer, Seat, build_economy
from bazaar_sim.world import run_world
from tests.sim.test_server import RULES, RUN, RawClient

# --- the codec ---------------------------------------------------------------


def test_garbage_and_empty_messages_are_bad_messages():
    with pytest.raises(codec.BadMessage, match="not a ClientMessage"):
        codec.decode_client_message(b"\xff\xff\xff")
    with pytest.raises(codec.BadMessage, match="selected no arm"):
        codec.decode_client_message(b"")


def test_a_command_missing_fields_is_refused_with_its_request_id():
    message = bazaar_pb2.ClientMessage()
    message.accept.request_id = "a-9"
    message.accept.run_id = RUN

    with pytest.raises(codec.BadMessage) as refused:
        codec.decode_client_message(message.SerializePartialToString())

    assert refused.value.request_id == "a-9" and "missing required fields" in str(refused.value)


def test_an_unknown_resource_is_a_bad_message():
    with pytest.raises(codec.BadMessage, match="unknown resource"):
        codec._resources(SimpleNamespace(items=[9]))


def test_every_command_decodes_to_its_action_and_retries_share_a_fingerprint():
    withdraw = mappers.build_withdraw(RUN, "w-1", "offer-3")
    first = codec.decode_client_message(withdraw.SerializeToString())
    again = codec.decode_client_message(withdraw.SerializeToString())

    assert first.action == WithdrawAction("offer-3") and first.request_id == "w-1"
    assert first.fingerprint == again.fingerprint


# --- the server --------------------------------------------------------------


@pytest.fixture
async def small_server():
    server = BazaarServer(build_economy(2, rules=RULES, starting_stock=10, run_id=RUN),
                          tick_ms=0, start_when=0)
    await server.start()
    yield server
    await server.stop()


async def test_text_frames_and_undecodable_bytes_are_refused_without_closing(small_server):
    async with RawClient(small_server, "P01") as p01:
        await p01.ws.send("hello")
        text = await p01.answer()
        await p01.ws.send(b"\xff\xff")
        garbage = await p01.answer()
        await p01.send(mappers.build_sync(RUN))
        state = await p01.recv()

    assert text.code is garbage.code is ControlCode.BAD_MESSAGE
    assert not text.close_session and state.run_id == RUN


async def test_commands_before_the_run_starts_get_run_not_running(small_server):
    async with RawClient(small_server, "P01") as p01:
        await p01.send(mappers.build_advertise(RUN, "ad-1", [Resource.WATER], [Resource.FOOD], 2))
        answer = await p01.answer()

    assert answer.code is ResultCode.RUN_NOT_RUNNING


async def test_stored_results_are_capped_per_planet():
    economy = build_economy(2, rules=replace(RULES, max_request_records_per_station=1), run_id=RUN)
    server = BazaarServer(economy, tick_ms=0, start_when=0)
    await server.start()
    try:
        async with RawClient(server, "P01") as p01:
            await server.start_run()
            await server.start_run()  # already running: nothing changes
            for n in range(2):
                await p01.send(mappers.build_advertise(RUN, f"ad-{n}", [Resource.WATER], [], 2))
            first, second = await p01.answer(), await p01.answer()
    finally:
        await server.stop()

    assert first.ok and second.code is ControlCode.REQUEST_CAPACITY_EXCEEDED
    assert not second.close_session


async def test_open_auth_refuses_a_planet_too_many_and_frees_seats_on_disconnect():
    server = BazaarServer(build_economy(1 + 2, rules=RULES, run_id="open"), open_auth=True,
                          tick_ms=0, start_when=0)
    await server.start()

    def anyone():
        return ClientConfig(ws_url=server.url(), token=Secret("x"), station_id="?")

    try:
        async with BazaarSession(anyone()) as a, BazaarSession(anyone()) as b, \
                   BazaarSession(anyone()) as c:
            await asyncio.gather(a.handshake(), b.handshake(), c.handshake())
            with pytest.raises(InvalidStatus) as full:
                async with BazaarSession(anyone()):
                    pass
        await asyncio.sleep(0.1)
        async with BazaarSession(anyone()) as again:
            first, _ = await again.handshake()
    finally:
        await server.stop()

    assert full.value.response.status_code == 403
    assert first.self_station_id == "P01"


async def test_the_run_starts_itself_when_enough_planets_are_ready_and_ticks_to_the_end():
    economy = build_economy(2, rules=replace(RULES, duration_ticks=3), run_id=RUN)
    server = BazaarServer(economy, tick_ms=20, start_when=1)
    await server.start()
    try:
        async with RawClient(server, "P01"):
            await asyncio.wait_for(server.finished.wait(), 5)
            await server.advance()  # after the end: nothing happens
    finally:
        await server.stop()

    assert server.phase is Phase.FINISHED and economy.tick == 3
    assert server.report()["score"]["planets"] == 2  # no report_path: returned, not written


async def test_a_message_to_a_planet_that_just_left_is_dropped_quietly():
    class Gone:
        async def send(self, payload):
            raise ConnectionClosedOK(None, None)

    server = BazaarServer(build_economy(2, rules=RULES), tick_ms=0)
    await server._send(Seat("P01", Gone()), b"state")  # no exception


def test_the_server_command_reports_the_final_score(monkeypatch, capsys):
    async def finished(args):
        return {"run_id": "r", "score": {"survivors": 3, "planets": 3, "world_alive_ticks": 360,
                                         "collective_success": True}}

    monkeypatch.setattr(server_module, "serve_run", finished)

    assert server_module.main(["--planets", "3"]) == 0
    assert "3/3 planets survived" in capsys.readouterr().out


def test_the_server_command_stops_cleanly_on_ctrl_c(monkeypatch):
    async def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(server_module, "serve_run", interrupted)
    assert server_module.main([]) == 130


async def test_serve_run_writes_credentials_and_returns_the_report(tmp_path):
    args = server_module.build_parser().parse_args([
        "--planets", "3", "--ticks", "2", "--tick-ms", "20", "--port", "0", "--start-when", "1",
        "--production", "run2", "--run-id", RUN, "--credentials-file", str(tmp_path / "c.json"),
        "--report", str(tmp_path / "r.json")])
    running = asyncio.create_task(server_module.serve_run(args))
    while not (tmp_path / "c.json").exists():
        await asyncio.sleep(0.05)
    credentials = json.loads((tmp_path / "c.json").read_text())
    token = credentials["players"][0]["token"]
    port = _port_of(running)
    async with BazaarSession(ClientConfig(ws_url=f"ws://127.0.0.1:{port}/ws",
                                          token=Secret(token), station_id="P01")) as p01:
        await p01.handshake()
        report = await asyncio.wait_for(running, 5)

    assert report["phase"] == "FINISHED" and (tmp_path / "r.json").exists()
    assert [p["station_id"] for p in credentials["players"]] == ["P01", "P02", "P03"]


def _port_of(task) -> int:
    """The port the task's server bound, found through the live websockets servers."""
    import gc

    for obj in gc.get_objects():
        if isinstance(obj, BazaarServer) and obj._server is not None and obj.economy.run_id == RUN:
            return obj.port
    raise AssertionError("server not found")


# --- the engine --------------------------------------------------------------


def two_planets(**rules):
    return SimulatedEconomy(
        SimStation("P01", Resource.WATER, Bundle(10, 10, 10)),
        SimStation("P02", Resource.FOOD, Bundle(10, 10, 10)),
        rules=replace(DEFAULT_RULES, **rules),
    )


def test_withdrawing_advertisements_follows_ownership():
    world = two_planets()
    ad = world.execute("P01", AdvertiseAction(frozenset({Resource.WATER}), frozenset(), 5)).object_id

    assert world.execute("P02", WithdrawAction(ad)).code is ResultCode.INVALID_ARGUMENT
    assert world.execute("P01", WithdrawAction(ad)).ok and world.advertisements == []
    assert world.execute("P01", WithdrawAction("nothing")).code is ResultCode.NOT_FOUND


def test_accepts_are_refused_once_expired_or_when_a_party_has_failed():
    world = two_planets()
    late = world.execute("P01", OfferAction("P02", Bundle(water=1), Bundle(food=1), 2)).object_id
    doomed = world.execute("P01", OfferAction("P02", Bundle(water=1), Bundle(food=1), 5)).object_id
    world.tick = 2  # past the first deadline, before expiry is processed

    assert world.execute("P02", AcceptAction(late)).code is ResultCode.EXPIRED
    world.stations["P01"].failed_once = True
    assert world.execute("P02", AcceptAction(doomed)).code is ResultCode.STATION_FAILED


def test_an_unknown_command_kind_is_an_invalid_argument():
    assert two_planets().execute("P01", SyncAction()).code is ResultCode.INVALID_ARGUMENT


def test_the_end_of_the_run_marks_open_offers_run_ended():
    world = two_planets()
    world.execute("P01", OfferAction("P02", Bundle(water=1), Bundle(food=1), 5))
    world.end_run()

    assert world.offers[0].status is OfferStatus.RUN_ENDED


def test_a_world_summary_names_each_planet_and_its_fate():
    """With only passive partners nobody can import, so everyone fails together."""
    early = run_world([OurPolicy(), Passive(), Passive()], ticks=10)
    late = run_world([OurPolicy(), Passive(), Passive()], ticks=45)

    assert early.summary().startswith("P01:ours=alive/hp")
    assert late.summary() == "P01:ours=t40/hp0, P02:passive=t40/hp0, P03:passive=t40/hp0"


# --- the benchmark -----------------------------------------------------------


def test_self_play_copies_the_candidate_onto_every_planet():
    row = benchmark.run_case(benchmark.Case(benchmark.SELF_PLAY, 1, 0, 3, "reserve-trader", ticks=30))

    assert row["planets"] == 3 and row["score"]["planets"] == 3


def test_a_check_on_another_build_says_so_and_a_mismatch_fails(tmp_path, capsys):
    result = benchmark.run_benchmark(
        benchmark.build_cases(["fair-field"], [1], [0], ["passive"], [3], ticks=20))
    result["meta"]["build"] = "elsewhere@000000000000"
    result["rows"][0]["transactions"] += 1
    path = tmp_path / "b.json"
    path.write_text(json.dumps(result))

    assert benchmark.main(["--check", str(path)]) == 1
    out = capsys.readouterr().out
    assert "note: recorded on elsewhere@000000000000" in out and "reproduced 0 of 1" in out


def test_a_seat_outside_the_field_is_refused(capsys):
    assert benchmark.main(["--slots", "9"]) == 2
    assert "--slots must be between 0 and 8" in capsys.readouterr().err


def test_head_to_head_skips_baselines_that_did_not_play():
    rows = [benchmark.run_case(benchmark.Case("fair-field", 1, 0, 9, "reserve-trader", ticks=10))]

    assert benchmark.summarize(rows)["versus_baselines"] == []


# --- the orchestrator --------------------------------------------------------


async def test_waiting_for_the_server_notices_an_early_exit_and_a_timeout(tmp_path):
    finished = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await finished.wait()
    with pytest.raises(RuntimeError, match="exited early"):
        await orchestrate._wait_for_file(tmp_path / "never", finished, timeout=1)

    sleeping = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    try:
        with pytest.raises(TimeoutError):
            await orchestrate._wait_for_file(tmp_path / "never", sleeping, timeout=0.2)
    finally:
        await orchestrate._terminate(sleeping)
    assert sleeping.returncode is not None


def test_a_summary_without_a_server_report_points_at_the_log(capsys):
    orchestrate.print_summary({"server_report": None})

    assert "see server.log" in capsys.readouterr().out


def test_an_impossible_strategy_mix_is_a_configuration_error(capsys):
    assert orchestrate.main(["--planets", "3", "--strategies", "reserve-trader:1"]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_dashboards_are_only_built_for_planets_that_logged_something(tmp_path):
    (tmp_path / "P01-evidence.jsonl").write_text("")

    assert orchestrate._summaries(tmp_path, ["P01", "P02"]) == {}
