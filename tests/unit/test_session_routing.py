"""Session event routing, driven through a fake transport.

Covers what the state machine cannot: that snapshots replace the view, that a
command's waiter resolves on either a result or a protocol_error, and that the
receive loop keeps running while a command is outstanding.
"""

from __future__ import annotations

import asyncio

import bazaar_pb2
import pytest

from bazaar_client.app import BazaarSession, SessionClosedError
from bazaar_client.config import ClientConfig, Secret
from bazaar_client.connection.ws_client import ConnectionClosedSentinel
from bazaar_client.domain import mappers
from bazaar_client.domain.types import Bundle, ControlCode, Resource, ResultCode
from tests.fixtures import factories


class FakeConnection:
    """Stands in for the websocket: the test pushes events, the session reacts."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def send(self, message, max_bytes=None) -> bytes:
        self.sent.append(message)
        return message.SerializeToString()

    async def send_payload(self, payload: bytes) -> bytes:
        message = bazaar_pb2.ClientMessage()
        message.ParseFromString(payload)
        self.sent.append(message)
        return payload

    async def recv_loop(self, queue: asyncio.Queue) -> None:
        while True:
            event = await self.inbox.get()
            if isinstance(event, ConnectionClosedSentinel):
                await queue.put(event)
                return
            await queue.put(event)


def make_config() -> ClientConfig:
    return ClientConfig(ws_url="ws://test/ws", token=Secret("t"), station_id="P01")


async def start_session(connection: FakeConnection) -> BazaarSession:
    session = BazaarSession(make_config(), connection=connection)
    await session.start()
    return session


async def push(connection: FakeConnection, event, session: BazaarSession | None = None) -> None:
    """Deliver an event and let it travel inbox -> recv loop -> pump -> handler."""
    await connection.inbox.put(event)
    for _ in range(100):
        await asyncio.sleep(0)
        queues_drained = connection.inbox.empty() and (
            session is None or session._queue.empty()
        )
        if queues_drained:
            break
    # A few more passes so the consumer finishes handling the last item.
    for _ in range(5):
        await asyncio.sleep(0)


async def complete_handshake(connection: FakeConnection, session: BazaarSession, **kwargs):
    snapshot = factories.make_snapshot(**kwargs)
    await push(connection, snapshot)
    await push(connection, factories.make_readiness(
        run_id=snapshot.run_id, snapshot_sequence=snapshot.snapshot_sequence
    ))
    return snapshot


# --- handshake ------------------------------------------------------------


async def test_handshake_sends_ready_and_returns_the_confirmation():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        snapshot = factories.make_snapshot(snapshot_sequence=1)
        await push(connection, snapshot)
        await push(connection, factories.make_readiness(run_id=snapshot.run_id, snapshot_sequence=1))

        got_snapshot, ack = await asyncio.wait_for(session.handshake(), timeout=1)

        assert got_snapshot.snapshot_sequence == 1
        assert ack.ready is True
        assert ack.snapshot_sequence == 1
    finally:
        await session.stop()


async def test_ready_message_declares_the_sequence_it_read():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session, snapshot_sequence=1)

        ready_messages = [m for m in connection.sent if m.WhichOneof("message") == "ready"]
        assert len(ready_messages) == 1
        assert ready_messages[0].ready.ready is True
        assert ready_messages[0].ready.snapshot_sequence == 1
        assert ready_messages[0].ready.protocol_version == "2.0"
    finally:
        await session.stop()


async def test_session_becomes_ready_and_allows_trading_while_running():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)

        assert session.lifecycle.is_ready
        assert session.lifecycle.can_send_trading_commands()
    finally:
        await session.stop()


# --- snapshot replacement -------------------------------------------------


async def test_newer_snapshot_replaces_the_view():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session, snapshot_sequence=1)
        await push(connection, factories.make_snapshot(snapshot_sequence=2, world_version=9))

        assert session.latest_snapshot.snapshot_sequence == 2
        assert session.latest_snapshot.world_version == 9
    finally:
        await session.stop()


async def test_stale_or_duplicate_snapshot_is_ignored():
    """A replayed snapshot must not roll the view backwards."""
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session, snapshot_sequence=1)
        await push(connection, factories.make_snapshot(snapshot_sequence=5, world_version=9))
        await push(connection, factories.make_snapshot(snapshot_sequence=3, world_version=4))

        assert session.latest_snapshot.snapshot_sequence == 5
        assert session.latest_snapshot.world_version == 9
    finally:
        await session.stop()


async def test_waiting_for_a_later_snapshot_resolves_when_it_arrives():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session, snapshot_sequence=1)
        waiter = asyncio.create_task(session.wait_for_snapshot(min_sequence=2))
        await asyncio.sleep(0)

        await push(connection, factories.make_snapshot(snapshot_sequence=2))

        assert (await asyncio.wait_for(waiter, timeout=1)).snapshot_sequence == 2
    finally:
        await session.stop()


async def test_command_budget_tracks_the_rules_from_the_snapshot():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(
            connection, session, rules=factories.make_rules(new_commands_per_station_per_tick=7)
        )

        assert session._throttle.limit == 7
    finally:
        await session.stop()


# --- command outcomes -----------------------------------------------------


async def test_command_resolves_on_its_matching_result():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        message = mappers.build_advertise(session.run_id, "req-1", [Resource.WATER], [], 6)

        pending = asyncio.create_task(
            session.send_command(message, kind="advertise", request_id="req-1")
        )
        await asyncio.sleep(0)
        await push(connection, factories.make_result(request_id="req-1", object_id="ad-7"))

        outcome = await asyncio.wait_for(pending, timeout=1)

        assert outcome.ok
        assert outcome.result.object_id == "ad-7"
        assert outcome.code_name == "OK"
    finally:
        await session.stop()


async def test_command_resolves_on_a_protocol_error_instead_of_a_result():
    """README step 9: the refused command gets only a protocol_error."""
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        message = mappers.build_advertise(session.run_id, "req-9", [Resource.WATER], [], 6)

        pending = asyncio.create_task(
            session.send_command(message, kind="advertise", request_id="req-9")
        )
        await asyncio.sleep(0)
        await push(
            connection,
            factories.make_protocol_error(
                request_id="req-9",
                code=ControlCode.REQUEST_CAPACITY_EXCEEDED,
                close_session=False,
            ),
        )

        outcome = await asyncio.wait_for(pending, timeout=1)

        assert not outcome.ok
        assert outcome.result is None
        assert outcome.error.code is ControlCode.REQUEST_CAPACITY_EXCEEDED
        assert outcome.code_name == "REQUEST_CAPACITY_EXCEEDED"
        assert session.lifecycle.is_ready  # connection stays usable
    finally:
        await session.stop()


async def test_an_unsuccessful_result_is_reported_not_treated_as_sent():
    """A rejected command is information, not "command sent"."""
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        message = mappers.build_offer(
            session.run_id, "req-2", "P02", Bundle(water=99), Bundle(food=1), 6
        )

        pending = asyncio.create_task(
            session.send_command(message, kind="offer", request_id="req-2")
        )
        await asyncio.sleep(0)
        await push(
            connection,
            factories.make_result(
                request_id="req-2",
                ok=False,
                code=ResultCode.INSUFFICIENT_RESOURCES,
                object_id=None,
            ),
        )

        outcome = await asyncio.wait_for(pending, timeout=1)

        assert not outcome.ok
        assert outcome.result.code is ResultCode.INSUFFICIENT_RESOURCES
    finally:
        await session.stop()


async def test_rate_limited_result_blocks_the_budget_until_retry_after_tick():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        message = mappers.build_advertise(session.run_id, "req-3", [Resource.WATER], [], 6)
        pending = asyncio.create_task(
            session.send_command(message, kind="advertise", request_id="req-3")
        )
        await asyncio.sleep(0)
        await push(
            connection,
            factories.make_result(
                request_id="req-3", ok=False, code=ResultCode.RATE_LIMITED, retry_after_tick=12
            ),
        )
        await asyncio.wait_for(pending, timeout=1)

        assert session._throttle.blocked_until_tick == 12
        assert session._throttle.remaining(tick=11) == 0
    finally:
        await session.stop()


async def test_reusing_an_id_for_new_content_is_blocked_before_transmitting():
    """The guard is worthless after the fact: the conflicting command would
    already be on the wire and the server would answer with a conflict."""
    from bazaar_client.connection.requests import RequestIdConflictError

    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        first = mappers.build_advertise(session.run_id, "req-1", [Resource.WATER], [], 6)
        pending = asyncio.create_task(
            session.send_command(first, kind="advertise", request_id="req-1")
        )
        await asyncio.sleep(0)
        await push(connection, factories.make_result(request_id="req-1"), session)
        await asyncio.wait_for(pending, timeout=1)
        sent_before = len(connection.sent)

        changed = mappers.build_advertise(
            session.run_id, "req-1", [Resource.COMPONENTS], [], 6
        )
        with pytest.raises(RequestIdConflictError):
            await session.send_command(changed, kind="advertise", request_id="req-1")

        assert len(connection.sent) == sent_before, "conflicting command was transmitted"
    finally:
        await session.stop()


async def test_an_exact_retry_is_allowed_through():
    """Resending the identical command with its id recovers the stored result."""
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        message = mappers.build_advertise(session.run_id, "req-1", [Resource.WATER], [], 6)

        for _ in range(2):
            pending = asyncio.create_task(
                session.send_command(message, kind="advertise", request_id="req-1")
            )
            await asyncio.sleep(0)
            await push(connection, factories.make_result(request_id="req-1"), session)
            outcome = await asyncio.wait_for(pending, timeout=1)
            assert outcome.ok
    finally:
        await session.stop()


async def test_a_result_for_an_unknown_request_is_ignored_without_crashing():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        await push(connection, factories.make_result(request_id="never-sent"))

        assert session.lifecycle.is_ready
    finally:
        await session.stop()


async def test_closing_the_connection_fails_outstanding_waiters():
    """A pending command must not hang forever when the socket goes away."""
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        await complete_handshake(connection, session)
        waiter = asyncio.create_task(session.wait_for_snapshot(min_sequence=99))
        await asyncio.sleep(0)

        await push(connection, ConnectionClosedSentinel())

        with pytest.raises(SessionClosedError):
            await asyncio.wait_for(waiter, timeout=1)
    finally:
        await session.stop()


async def test_run_id_is_unknown_before_the_first_state():
    connection = FakeConnection()
    session = await start_session(connection)
    try:
        with pytest.raises(SessionClosedError):
            _ = session.run_id
    finally:
        await session.stop()
