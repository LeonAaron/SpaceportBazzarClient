"""A Bazaar server of our own: the real wire protocol over our economy model.

    python -m bazaar_sim.server --planets 3 --ticks 60 --tick-ms 1000

Any client that speaks `bazaar.protobuf.v2` can play -- ours, several copies
of ours, or another pair's. Each planet gets a token in a credentials file in
the same format the practice server writes, so our client finds its key with
`--credentials-file`. With `--open-auth` the token is ignored and planets are
assigned in connection order instead.

The run starts when `--start-when` planets have confirmed readiness (default:
all of them), ticks every `--tick-ms`, and writes a scored JSON report when it
finishes. What it implements and what it leaves out: SIMULATOR.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from bazaar_client.config import SUBPROTOCOL
from bazaar_client.domain.mappers import PROTOCOL_VERSION
from bazaar_client.domain.types import (
    Bundle,
    CommandResult,
    ControlCode,
    Phase,
    ReadinessAck,
    ResultCode,
)
from bazaar_client.version import build_info
from bazaar_sim import codec
from bazaar_sim.economy import DEFAULT_RULES, SimStation, SimulatedEconomy
from bazaar_sim.world import (
    Production,
    balanced_production,
    run_two_production,
    score_economy,
    specialty_of,
)

logger = logging.getLogger("bazaar_sim.server")

NAMES = ("Aqua", "Verdant", "Forge", "Tidal", "Harvest", "Anvil", "Reef", "Orchard", "Foundry")


@dataclass
class Seat:
    """One planet's live connection: its own snapshot counter and readiness."""

    station_id: str
    connection: ServerConnection
    sequence: int = 0
    ready: bool = False


@dataclass
class StoredResult:
    fingerprint: str
    result: CommandResult


@dataclass
class ServerStats:
    commands: dict[str, int] = field(default_factory=dict)
    results: dict[str, int] = field(default_factory=dict)
    protocol_errors: dict[str, int] = field(default_factory=dict)
    connections: int = 0

    def count(self, table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1


def build_economy(
    planets: int, *, rules=DEFAULT_RULES, starting_stock: int = 30, run_id: str | None = None
) -> SimulatedEconomy:
    stations = [
        SimStation(
            f"P{i + 1:02}", specialty_of(i), Bundle(starting_stock, starting_stock, starting_stock),
            display_name=NAMES[i % len(NAMES)] + (f"-{i // len(NAMES) + 1}" if i >= len(NAMES) else ""),
        )
        for i in range(planets)
    ]
    return SimulatedEconomy(*stations, rules=rules, run_id=run_id or f"sim-{secrets.token_hex(6)}")


class BazaarServer:
    def __init__(
        self,
        economy: SimulatedEconomy,
        *,
        tokens: dict[str, str] | None = None,
        open_auth: bool = False,
        tick_ms: int = 1000,
        start_when: int | None = None,
        production: Production | None = None,
        report_path: Path | None = None,
    ) -> None:
        self.economy = economy
        self.tokens = tokens if tokens is not None else {
            secrets.token_urlsafe(16): sid for sid in economy.stations
        }
        self.open_auth = open_auth
        self.tick_ms = tick_ms
        self.start_when = len(economy.stations) if start_when is None else start_when
        self.production = production
        self.report_path = report_path
        self.phase = Phase.READY
        self.seats: dict[str, Seat] = {}
        self.results: dict[str, dict[str, StoredResult]] = {sid: {} for sid in economy.stations}
        self.stats = ServerStats()
        self.finished = asyncio.Event()
        self._server: Server | None = None
        self._ticker: asyncio.Task | None = None
        self._claimed: set[str] = set()
        self._started_at: float | None = None
        self._apply_production()

    # --- lifecycle --------------------------------------------------------

    @property
    def port(self) -> int:
        assert self._server is not None, "server not started"
        return self._server.sockets[0].getsockname()[1]

    def url(self, host: str = "127.0.0.1") -> str:
        return f"ws://{host}:{self.port}/ws"

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = await serve(
            self._handle, host, port, subprotocols=[SUBPROTOCOL],
            process_request=self._authenticate, ping_interval=None,
        )
        logger.info("listening on %s, run %s, %d planets", self.url(host), self.economy.run_id,
                    len(self.economy.stations))

    async def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def credentials(self) -> dict:
        """Same shape as the practice server's validation-credentials.json."""
        by_station = {sid: token for token, sid in self.tokens.items()}
        return {
            "run_id": self.economy.run_id,
            "players": [
                {"station_id": sid, "token": by_station.get(sid, ""),
                 "display_name": station.display_name}
                for sid, station in self.economy.stations.items()
            ],
        }

    # --- handshake --------------------------------------------------------

    def _authenticate(self, connection: ServerConnection, request: Request) -> Response | None:
        offered = ",".join(request.headers.get_all("Sec-WebSocket-Protocol"))
        if SUBPROTOCOL not in [p.strip() for p in offered.split(",")]:
            return connection.respond(HTTPStatus.BAD_REQUEST, f"subprotocol {SUBPROTOCOL} required\n")
        header = request.headers.get("Authorization", "")
        token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if self.open_auth:
            free = [sid for sid in self.economy.stations if sid not in self._claimed]
            if not free:
                return connection.respond(HTTPStatus.FORBIDDEN, "every planet is taken\n")
            station_id = free[0]
            self._claimed.add(station_id)
        else:
            station_id = self.tokens.get(token)
            if station_id is None:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "unknown token\n")
        connection.station_id = station_id  # read back in _handle
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        station_id = connection.station_id
        previous = self.seats.get(station_id)
        if previous is not None:
            # A new connection replaces the old one, as on the real server.
            await self._send(previous, codec.encode_protocol_error(
                ControlCode.SESSION_FENCED, run_id=self.economy.run_id, request_id=None,
                close_session=True))
            await previous.connection.close()
        seat = Seat(station_id, connection)
        self.seats[station_id] = seat
        self.stats.connections += 1
        logger.info("%s connected", station_id)
        try:
            await self._send_state(seat)
            async for raw in connection:
                if isinstance(raw, str):
                    await self._protocol_error(seat, ControlCode.BAD_MESSAGE, None, close=False)
                    continue
                if not await self._on_message(seat, raw):
                    break
        except ConnectionClosed:
            pass
        finally:
            if self.seats.get(station_id) is seat:
                del self.seats[station_id]
                if self.open_auth:
                    self._claimed.discard(station_id)
            logger.info("%s disconnected", station_id)

    # --- messages ---------------------------------------------------------

    async def _on_message(self, seat: Seat, raw: bytes) -> bool:
        """Handle one message; False closes the connection."""
        try:
            message = codec.decode_client_message(raw)
        except codec.BadMessage as exc:
            logger.info("%s sent a bad message: %s", seat.station_id, exc)
            await self._protocol_error(seat, ControlCode.BAD_MESSAGE, exc.request_id, close=False)
            return True
        if message.protocol_version != PROTOCOL_VERSION:
            await self._protocol_error(seat, ControlCode.UNSUPPORTED_VERSION, message.request_id, close=True)
            return False
        if message.run_id != self.economy.run_id:
            await self._protocol_error(seat, ControlCode.RUN_MISMATCH, message.request_id, close=True)
            return False

        if message.kind == "ready":
            seat.ready = bool(message.ready)
            await self._send(seat, codec.encode_readiness(ReadinessAck(
                self.economy.run_id, seat.ready, message.snapshot_sequence or 0)))
            self._maybe_start()
            return True
        if message.kind == "sync":
            await self._send_state(seat)
            return True
        if not seat.ready:
            # Trading before readiness: refused, but the session stays usable.
            await self._protocol_error(seat, ControlCode.BAD_MESSAGE, message.request_id, close=False)
            return True
        await self._command(seat, message)
        return True

    async def _command(self, seat: Seat, message: codec.ClientCommand) -> None:
        stored = self.results[seat.station_id]
        previous = stored.get(message.request_id)
        if previous is not None:
            if previous.fingerprint == message.fingerprint:
                await self._send(seat, codec.encode_result(previous.result, self.economy.run_id))
                await self._send_state(seat)
            else:
                await self._send_result(seat, self._result(message.request_id, ResultCode.REQUEST_ID_CONFLICT))
            return
        if len(stored) >= self.economy.rules.max_request_records_per_station:
            await self._protocol_error(seat, ControlCode.REQUEST_CAPACITY_EXCEEDED,
                                       message.request_id, close=False)
            return

        self.stats.count(self.stats.commands, message.kind)
        if self.phase is not Phase.RUNNING:
            outcome_code, object_id, transaction_id = ResultCode.RUN_NOT_RUNNING, None, None
        else:
            outcome = self.economy.execute(seat.station_id, message.action)
            outcome_code, object_id, transaction_id = (
                outcome.code, outcome.object_id, outcome.transaction_id)
        if outcome_code is ResultCode.OK:
            self.economy.world_version += 1
        result = self._result(message.request_id, outcome_code, object_id, transaction_id)
        stored[message.request_id] = StoredResult(message.fingerprint, result)
        await self._send_result(seat, result)
        await self.broadcast()

    def _result(self, request_id, code, object_id=None, transaction_id=None) -> CommandResult:
        return CommandResult(
            request_id=request_id,
            ok=code is ResultCode.OK,
            code=code,
            processed_tick=self.economy.tick,
            processed_version=self.economy.world_version,
            object_id=object_id,
            transaction_id=transaction_id,
            retry_after_tick=self.economy.tick + 1 if code is ResultCode.RATE_LIMITED else None,
        )

    async def _send_result(self, seat: Seat, result: CommandResult) -> None:
        self.stats.count(self.stats.results, result.code.name)
        await self._send(seat, codec.encode_result(result, self.economy.run_id))

    async def _protocol_error(self, seat: Seat, code: ControlCode, request_id, *, close: bool) -> None:
        self.stats.count(self.stats.protocol_errors, code.name)
        await self._send(seat, codec.encode_protocol_error(
            code, run_id=self.economy.run_id, request_id=request_id, close_session=close))
        if close:
            await seat.connection.close()

    # --- states -----------------------------------------------------------

    def snapshot_for(self, seat: Seat):
        seat.sequence += 1
        station_id = seat.station_id
        outcome = self.economy.outcome_for(station_id) if self.phase is Phase.FINISHED else None
        return self.economy.observation_for(
            station_id, sequence=seat.sequence, phase=self.phase,
            request_results=tuple(r.result for r in self.results[station_id].values()),
            outcome=outcome,
        )

    async def _send_state(self, seat: Seat) -> None:
        await self._send(seat, codec.encode_state(self.snapshot_for(seat)))

    async def broadcast(self) -> None:
        for seat in list(self.seats.values()):
            await self._send_state(seat)

    async def _send(self, seat: Seat, payload: bytes) -> None:
        try:
            await seat.connection.send(payload)
        except ConnectionClosed:
            logger.debug("%s went away before a message could be sent", seat.station_id)

    # --- time -------------------------------------------------------------

    def _maybe_start(self) -> None:
        ready = sum(seat.ready for seat in self.seats.values())
        if self.phase is Phase.READY and self.start_when > 0 and ready >= self.start_when:
            asyncio.get_running_loop().create_task(self.start_run())

    async def start_run(self) -> None:
        if self.phase is not Phase.READY:
            return
        self.phase = Phase.RUNNING
        self._started_at = time.monotonic()
        self.economy.world_version += 1
        logger.info("run %s started", self.economy.run_id)
        await self.broadcast()
        if self.tick_ms > 0:
            self._ticker = asyncio.get_running_loop().create_task(self._tick_forever())

    async def _tick_forever(self) -> None:
        while self.phase is Phase.RUNNING:
            await asyncio.sleep(self.tick_ms / 1000)
            await self.advance()

    def _apply_production(self) -> None:
        if self.production is None:
            return
        for i, station in enumerate(self.economy.stations.values()):
            station.production = self.production(self.economy.tick, i)

    async def advance(self) -> None:
        """One tick: production, upkeep, expiry, failure, then everyone's new state."""
        if self.phase is not Phase.RUNNING:
            return
        self.economy.advance_tick()
        self._apply_production()
        if self.economy.finished:
            self.economy.end_run()
            self.phase = Phase.FINISHED
            logger.info("run %s finished at tick %d", self.economy.run_id, self.economy.tick)
            self.write_report()
            await self.broadcast()
            self.finished.set()
            return
        await self.broadcast()

    # --- report -----------------------------------------------------------

    def report(self) -> dict:
        economy = self.economy
        score = score_economy(economy, economy.rules.duration_ticks)
        return {
            "run_id": economy.run_id,
            "server": build_info(),
            "phase": self.phase.name,
            "tick": economy.tick,
            "duration_ticks": economy.rules.duration_ticks,
            "wall_seconds": None if self._started_at is None
            else round(time.monotonic() - self._started_at, 3),
            "score": score.as_dict(),
            "planets": [
                {
                    "station_id": sid,
                    "display_name": s.display_name,
                    "specialty": s.specialty.name,
                    "failed": s.failed_once,
                    "first_failure_tick": s.first_failure_tick,
                    "health": s.health,
                    "inventory": s.inventory.as_dict(),
                    "produced": s.produced_total.as_dict(),
                    "consumed": s.consumed_total.as_dict(),
                    "unmet": s.unmet_total.as_dict(),
                    "imported": s.imported.as_dict(),
                    "exported": s.exported.as_dict(),
                    "shortage_ticks": s.shortage_ticks,
                }
                for sid, s in economy.stations.items()
            ],
            "transactions": len(economy.transactions),
            "commands": self.stats.commands,
            "results": self.stats.results,
            "protocol_errors": self.stats.protocol_errors,
            "connections": self.stats.connections,
        }

    def write_report(self) -> None:
        if self.report_path is None:
            return
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(json.dumps(self.report(), indent=2), encoding="utf-8")
        logger.info("report written to %s", self.report_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bazaar-sim-server", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3100)
    parser.add_argument("--planets", type=int, default=3)
    parser.add_argument("--ticks", type=int, default=120, help="run duration in ticks")
    parser.add_argument("--tick-ms", type=int, default=1000, help="wall time per tick")
    parser.add_argument("--starting-stock", type=int, default=30)
    parser.add_argument("--production", choices=("balanced", "run2"), default="balanced",
                        help="balanced: output exactly covers world upkeep; run2: 2/5/6 phases")
    parser.add_argument("--variation", type=int, default=0,
                        help="balanced only: +/- swing per 12-tick phase, zero on average")
    parser.add_argument("--start-when", type=int, default=None,
                        help="start once this many planets are ready (default: all)")
    parser.add_argument("--open-auth", action="store_true",
                        help="ignore tokens; hand out planets in connection order")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--credentials-file", type=Path, default=Path("sim-credentials.json"))
    parser.add_argument("--report", type=Path, default=Path("sim-report.json"))
    parser.add_argument("--log-level", default="INFO")
    return parser


async def serve_run(args: argparse.Namespace) -> dict:
    rules = replace(DEFAULT_RULES, duration_ticks=args.ticks, tick_duration_ms=args.tick_ms)
    economy = build_economy(args.planets, rules=rules, starting_stock=args.starting_stock,
                            run_id=args.run_id)
    production = (balanced_production(args.planets, variation=args.variation)
                  if args.production == "balanced" else run_two_production())
    server = BazaarServer(economy, open_auth=args.open_auth, tick_ms=args.tick_ms,
                          start_when=args.start_when, production=production,
                          report_path=args.report)
    await server.start(args.host, args.port)
    args.credentials_file.write_text(json.dumps(server.credentials(), indent=2), encoding="utf-8")
    print(f"Bazaar test server on {server.url(args.host)}", flush=True)
    print(f"credentials: {args.credentials_file}  run: {economy.run_id}", flush=True)
    print(f"waiting for {server.start_when} of {args.planets} planets to be ready", flush=True)
    try:
        await server.finished.wait()
    finally:
        await server.stop()
    return server.report()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    try:
        report = asyncio.run(serve_run(args))
    except KeyboardInterrupt:
        return 130
    score = report["score"]
    print(f"run {report['run_id']} finished: {score['survivors']}/{score['planets']} planets "
          f"survived, world alive ticks {score['world_alive_ticks']}, "
          f"collective success {score['collective_success']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
