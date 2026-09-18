"""Session wiring: transport, lifecycle, request tracking and event routing.

The receive loop runs as its own task for the whole session, so the client keeps
reading while it waits for any particular answer. Commands await a future that
the router resolves, which is also how a command answered by a protocol_error
instead of a result stays unblocked.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bazaar_client.config import ClientConfig
from bazaar_client.connection.lifecycle import (
    Abort,
    Reconnect,
    SendReady,
    SessionLifecycle,
)
from bazaar_client.connection.requests import (
    PendingRequestTracker,
    RequestIdGenerator,
    body_fingerprint,
)
from bazaar_client.connection.throttle import CommandThrottle
from bazaar_client.connection.ws_client import (
    BazaarConnection,
    ConnectionClosedSentinel,
)
from bazaar_client.domain import mappers
from bazaar_client.domain.types import (
    CommandResult,
    ProtocolErrorEvent,
    ReadinessAck,
    ResultCode,
    Snapshot,
)

logger = logging.getLogger(__name__)

SNAPSHOT_HISTORY = 128


class SessionClosedError(RuntimeError):
    """Raised when the session ended before an awaited answer arrived."""


class SessionAbortedError(RuntimeError):
    """Raised on a control error that retrying cannot fix."""


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """What came back for one command: a result, or a control-level refusal."""

    request_id: str
    result: CommandResult | None = None
    error: ProtocolErrorEvent | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.ok

    @property
    def code_name(self) -> str:
        if self.result is not None:
            return self.result.code.name
        if self.error is not None:
            return self.error.code.name
        return "NO_ANSWER"


class BazaarSession:
    """One connection's worth of state: snapshots in, commands out."""

    def __init__(self, config: ClientConfig, connection: BazaarConnection | None = None) -> None:
        self._config = config
        self._connection = connection or BazaarConnection(config)
        self._lifecycle = SessionLifecycle()
        self._tracker = PendingRequestTracker()
        self._throttle = CommandThrottle(limit_per_tick=1)
        self._ids: RequestIdGenerator | None = None

        self._queue: asyncio.Queue = asyncio.Queue()
        self._recv_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None

        self._latest: Snapshot | None = None
        # Several states can arrive back to back, so keep recent ones
        # individually retrievable rather than only the newest.
        self._by_sequence: dict[int, Snapshot] = {}
        self._snapshot_waiters: list[tuple[int, asyncio.Future]] = []
        self._command_waiters: dict[str, asyncio.Future] = {}
        self._readiness: ReadinessAck | None = None
        self._readiness_waiter: asyncio.Future | None = None
        self._closed = asyncio.Event()
        self._abort_reason: str | None = None

    # --- properties -------------------------------------------------------

    @property
    def lifecycle(self) -> SessionLifecycle:
        return self._lifecycle

    @property
    def latest_snapshot(self) -> Snapshot | None:
        return self._latest

    @property
    def run_id(self) -> str:
        if self._lifecycle.run_id is None:
            raise SessionClosedError("run id is unknown until the first state arrives")
        return self._lifecycle.run_id

    @property
    def request_ids(self) -> RequestIdGenerator:
        if self._ids is None:
            raise SessionClosedError("request ids need the station id from the first state")
        return self._ids

    # --- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> BazaarSession:
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    async def start(self) -> None:
        self._readiness = None  # every connection needs its own readiness exchange
        self._by_sequence.clear()  # sequences restart at 1 on a new connection
        await self._connection.connect()
        self._lifecycle.on_connected()
        self._recv_task = asyncio.create_task(
            self._connection.recv_loop(self._queue), name="bazaar-recv"
        )
        self._pump_task = asyncio.create_task(self._consume_events(), name="bazaar-pump")

    async def stop(self) -> None:
        for task in (self._pump_task, self._recv_task):
            if task is not None and not task.done():
                task.cancel()
        await self._connection.close()
        self._fail_all_waiters(SessionClosedError("session stopped"))

    # --- event routing ----------------------------------------------------

    async def _consume_events(self) -> None:
        while True:
            event = await self._queue.get()

            if isinstance(event, ConnectionClosedSentinel):
                self._closed.set()
                self._lifecycle.on_disconnected()
                self._fail_all_waiters(SessionClosedError("connection closed"))
                return
            if isinstance(event, BaseException):
                logger.error("dropping undecodable message: %s", event)
                continue

            try:
                directive = self._lifecycle.on_event(event)
            except Exception as exc:
                logger.error("lifecycle rejected an event: %s", exc)
                self._fail_all_waiters(exc)
                return

            self._route(event)

            if directive is not None:
                await self._apply(directive)

    def _route(self, event) -> None:
        if isinstance(event, Snapshot):
            self._on_snapshot(event)
        elif isinstance(event, CommandResult):
            self._on_result(event)
        elif isinstance(event, ReadinessAck):
            self._resolve_readiness(event)
        elif isinstance(event, ProtocolErrorEvent):
            self._on_protocol_error(event)

    def _on_snapshot(self, snapshot: Snapshot) -> None:
        # Replace the view wholesale; never re-apply transactions to inventory.
        if self._latest is not None and snapshot.snapshot_sequence <= self._latest.snapshot_sequence:
            logger.debug(
                "ignoring stale snapshot %d (have %d)",
                snapshot.snapshot_sequence,
                self._latest.snapshot_sequence,
            )
            return

        self._latest = snapshot
        self._by_sequence[snapshot.snapshot_sequence] = snapshot
        if len(self._by_sequence) > SNAPSHOT_HISTORY:
            for stale in sorted(self._by_sequence)[:-SNAPSHOT_HISTORY]:
                del self._by_sequence[stale]
        self._throttle.update_limit(snapshot.rules.new_commands_per_station_per_tick)
        if self._ids is None:
            self._ids = RequestIdGenerator(snapshot.self_station_id)

        still_waiting = []
        for target, future in self._snapshot_waiters:
            if snapshot.snapshot_sequence >= target and not future.done():
                future.set_result(snapshot)
            elif not future.done():
                still_waiting.append((target, future))
        self._snapshot_waiters = still_waiting

    def _on_result(self, result: CommandResult) -> None:
        self._tracker.resolve(result.request_id)
        if result.code is ResultCode.RATE_LIMITED:
            self._throttle.block_until(result.retry_after_tick)

        future = self._command_waiters.pop(result.request_id, None)
        if future is not None and not future.done():
            future.set_result(CommandOutcome(result.request_id, result=result))
        else:
            logger.debug("result for %s arrived with no waiter", result.request_id)

    def _on_protocol_error(self, event: ProtocolErrorEvent) -> None:
        logger.warning(
            "protocol_error %s (request_id=%s, close_session=%s)",
            event.code.name,
            event.request_id,
            event.close_session,
        )
        if event.request_id is None:
            return
        # A refused command gets no result, so its waiter resolves on the error.
        self._tracker.resolve(event.request_id)
        future = self._command_waiters.pop(event.request_id, None)
        if future is not None and not future.done():
            future.set_result(CommandOutcome(event.request_id, error=event))

    def _resolve_readiness(self, ack: ReadinessAck) -> None:
        # Latched: the pump may route this before handshake() asks for it.
        self._readiness = ack
        if self._readiness_waiter is not None and not self._readiness_waiter.done():
            self._readiness_waiter.set_result(ack)

    async def _apply(self, directive) -> None:
        if isinstance(directive, SendReady):
            await self._send_ready(directive)
        elif isinstance(directive, Abort):
            self._abort_reason = directive.reason
            self._fail_all_waiters(SessionAbortedError(directive.reason))
        elif isinstance(directive, Reconnect):
            logger.warning("reconnect required: %s", directive.reason)
            self._fail_all_waiters(SessionClosedError(directive.reason))

    async def _send_ready(self, directive: SendReady) -> None:
        logger.info(
            "declaring readiness for run %s at snapshot_sequence %d",
            directive.run_id,
            directive.snapshot_sequence,
        )
        await self._connection.send(
            mappers.build_ready(directive.run_id, True, directive.snapshot_sequence)
        )

    def _fail_all_waiters(self, error: BaseException) -> None:
        for _, future in self._snapshot_waiters:
            if not future.done():
                future.set_exception(error)
        self._snapshot_waiters.clear()

        for future in self._command_waiters.values():
            if not future.done():
                future.set_exception(error)
        self._command_waiters.clear()

        if self._readiness_waiter is not None and not self._readiness_waiter.done():
            self._readiness_waiter.set_exception(error)

    # --- awaitable operations --------------------------------------------

    async def wait_for_snapshot(self, min_sequence: int = 1, timeout: float = 15.0) -> Snapshot:
        if self._latest is not None and self._latest.snapshot_sequence >= min_sequence:
            return self._latest

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._snapshot_waiters.append((min_sequence, future))
        return await asyncio.wait_for(future, timeout)

    async def wait_for_sequence(self, sequence: int, timeout: float = 15.0) -> Snapshot:
        """Return the snapshot with exactly this sequence number.

        Distinct from `wait_for_snapshot`, which yields whatever is newest:
        when several states arrive together, verifying a documented sequence
        needs that specific one rather than the most recent.
        """
        if sequence in self._by_sequence:
            return self._by_sequence[sequence]

        await self.wait_for_snapshot(min_sequence=sequence, timeout=timeout)
        if sequence not in self._by_sequence:
            raise SessionClosedError(f"snapshot {sequence} was never delivered")
        return self._by_sequence[sequence]

    async def wait_for_readiness(self, timeout: float = 15.0) -> ReadinessAck:
        if self._readiness is not None:
            return self._readiness
        self._readiness_waiter = asyncio.get_running_loop().create_future()
        return await asyncio.wait_for(self._readiness_waiter, timeout)

    async def handshake(self, timeout: float = 15.0) -> tuple[Snapshot, ReadinessAck]:
        """Read the first state, declare readiness, wait for the confirmation."""
        snapshot = await self.wait_for_snapshot(min_sequence=1, timeout=timeout)
        ack = await self.wait_for_readiness(timeout=timeout)
        return snapshot, ack

    async def send_sync(self) -> None:
        """Request a fresh snapshot. It carries no request id, so nothing is awaited
        here; the answer arrives as an ordinary state message."""
        await self._connection.send(mappers.build_sync(self.run_id))
        logger.info("sent sync for run %s", self.run_id)

    async def send_command(self, message, kind: str, request_id: str, timeout: float = 15.0) -> CommandOutcome:
        """Send one command and await its result or its protocol-level refusal."""
        snapshot = self._latest
        tick = snapshot.tick if snapshot else 0
        max_bytes = snapshot.rules.max_command_bytes if snapshot else None

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._command_waiters[request_id] = future

        try:
            sent = await self._connection.send(message, max_bytes=max_bytes)
        except Exception:
            self._command_waiters.pop(request_id, None)
            raise

        self._tracker.register(request_id, body_fingerprint(sent), kind, tick)
        self._throttle.record_sent(tick)
        logger.info("sent %s request_id=%s (%d bytes)", kind, request_id, len(sent))

        return await asyncio.wait_for(future, timeout)
