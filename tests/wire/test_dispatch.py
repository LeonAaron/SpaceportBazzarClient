"""Outgoing command construction and incoming oneof dispatch.

The point of these tests is that the client never produces a message the server
would answer with CONTROL_CODE_BAD_MESSAGE.
"""

from __future__ import annotations

import bazaar_pb2
import pytest

from bazaar_client.codec.wire import (
    MessageTooLargeError,
    UninitializedMessageError,
    UnknownServerMessageError,
    decode_server_message,
    dispatch_server_message,
    encode_client_message,
)
from bazaar_client.domain import mappers
from bazaar_client.domain.types import (
    Bundle,
    CommandResult,
    ProtocolErrorEvent,
    ReadinessAck,
    Resource,
    Snapshot,
)
from tests.fixtures import factories

RUN = factories.RUN_ID


def parse_client(payload: bytes) -> bazaar_pb2.ClientMessage:
    message = bazaar_pb2.ClientMessage()
    message.ParseFromString(payload)
    return message


# --- outgoing commands ----------------------------------------------------


def test_advertise_sets_every_required_field():
    message = mappers.build_advertise(
        RUN, "student-advertise-1", [Resource.WATER], [Resource.FOOD], expires_tick=6
    )
    revived = parse_client(encode_client_message(message))

    assert revived.WhichOneof("message") == "advertise"
    assert revived.advertise.type == bazaar_pb2.ADVERTISE_TYPE_ADVERTISE
    assert revived.advertise.protocol_version == "2.0"
    assert revived.advertise.run_id == RUN
    assert revived.advertise.request_id == "student-advertise-1"
    assert list(revived.advertise.body.selling.items) == [bazaar_pb2.RESOURCE_WATER]
    assert list(revived.advertise.body.seeking.items) == [bazaar_pb2.RESOURCE_FOOD]
    assert revived.advertise.body.expires_tick == 6


def test_advertise_with_empty_selling_still_sends_the_container():
    """README step 3 sends `selling {}`; omitting the container would be invalid."""
    message = mappers.build_advertise(
        RUN, "student-advertise-seeking-1", [], [Resource.COMPONENTS], expires_tick=6
    )
    revived = parse_client(encode_client_message(message))

    assert revived.advertise.body.HasField("selling")
    assert list(revived.advertise.body.selling.items) == []
    assert list(revived.advertise.body.seeking.items) == [bazaar_pb2.RESOURCE_COMPONENTS]


def test_offer_sends_zero_quantities_explicitly():
    """A zero side of a bundle is a stated quantity, not an omitted field."""
    message = mappers.build_offer(
        RUN,
        "student-offer-1",
        recipient_id="P02",
        give=Bundle(water=2),
        receive=Bundle(food=1),
        expires_tick=6,
    )
    revived = parse_client(encode_client_message(message))
    body = revived.offer.body

    assert body.recipient_id == "P02"
    assert (body.give.water, body.give.food, body.give.components) == (2, 0, 0)
    assert (body.receive.water, body.receive.food, body.receive.components) == (0, 1, 0)
    assert body.give.HasField("food") and body.give.HasField("components")


def test_gift_offer_has_an_all_zero_receive():
    message = mappers.build_offer(
        RUN, "gift-1", "P02", Bundle(components=1), Bundle.zero(), expires_tick=6
    )
    revived = parse_client(encode_client_message(message))
    receive = revived.offer.body.receive

    assert (receive.water, receive.food, receive.components) == (0, 0, 0)
    assert receive.HasField("water")


def test_accept_and_withdraw_carry_their_ids():
    accept = parse_client(
        encode_client_message(mappers.build_accept(RUN, "student-accept-1", "offer-9"))
    )
    withdraw = parse_client(
        encode_client_message(mappers.build_withdraw(RUN, "student-withdraw-1", "ad-1"))
    )

    assert accept.accept.body.offer_id == "offer-9"
    assert accept.accept.type == bazaar_pb2.ACCEPT_TYPE_ACCEPT
    assert withdraw.withdraw.body.object_id == "ad-1"
    assert withdraw.withdraw.type == bazaar_pb2.WITHDRAW_TYPE_WITHDRAW


def test_sync_has_no_body_and_no_request_id():
    revived = parse_client(encode_client_message(mappers.build_sync(RUN)))

    assert revived.WhichOneof("message") == "sync"
    assert revived.sync.type == bazaar_pb2.SYNC_TYPE_SYNC
    assert revived.sync.run_id == RUN
    assert revived.sync.protocol_version == "2.0"
    assert not hasattr(revived.sync, "request_id")
    assert not hasattr(revived.sync, "body")


def test_ready_carries_the_snapshot_sequence_it_acknowledges():
    revived = parse_client(encode_client_message(mappers.build_ready(RUN, True, 1)))

    assert revived.ready.type == bazaar_pb2.READY_TYPE_READY
    assert revived.ready.ready is True
    assert revived.ready.snapshot_sequence == 1


def test_ready_false_is_transmitted_as_a_set_field():
    """`ready: false` is a real declaration, not an unset field."""
    revived = parse_client(encode_client_message(mappers.build_ready(RUN, False, 3)))

    assert revived.ready.HasField("ready")
    assert revived.ready.ready is False


# --- refusing to emit bad messages ---------------------------------------


def test_message_missing_a_required_field_is_refused_before_sending():
    message = bazaar_pb2.ClientMessage()
    message.accept.type = bazaar_pb2.ACCEPT_TYPE_ACCEPT
    message.accept.protocol_version = "2.0"
    message.accept.run_id = RUN
    # request_id and body deliberately absent.

    with pytest.raises(UninitializedMessageError, match="request_id"):
        encode_client_message(message)


def test_empty_client_message_is_refused():
    with pytest.raises(UninitializedMessageError):
        encode_client_message(bazaar_pb2.ClientMessage())


def test_oversized_command_is_refused_against_the_rules_limit():
    message = mappers.build_advertise(RUN, "x" * 64, [Resource.WATER], [], 6)

    with pytest.raises(MessageTooLargeError, match="limit is 8"):
        encode_client_message(message, max_bytes=8)


def test_command_within_the_limit_is_allowed():
    message = mappers.build_advertise(RUN, "ok-1", [Resource.WATER], [], 6)
    assert encode_client_message(message, max_bytes=16384)


# --- incoming dispatch ----------------------------------------------------


def test_dispatch_routes_each_server_arm_to_its_domain_type():
    cases = {
        "state": (mappers.snapshot_to_pb(factories.make_snapshot()), Snapshot),
        "result": (mappers.result_to_pb(factories.make_result(), RUN), CommandResult),
        "protocol_error": (
            mappers.protocol_error_to_pb(factories.make_protocol_error()),
            ProtocolErrorEvent,
        ),
        "readiness": (mappers.readiness_to_pb(factories.make_readiness()), ReadinessAck),
    }

    for arm, (payload, expected_type) in cases.items():
        message = bazaar_pb2.ServerMessage()
        getattr(message, arm).CopyFrom(payload)
        assert isinstance(decode_server_message(message.SerializeToString()), expected_type)


def test_dispatch_rejects_a_server_message_with_no_arm():
    with pytest.raises(UnknownServerMessageError):
        dispatch_server_message(bazaar_pb2.ServerMessage())


def test_result_decodes_with_its_request_id_for_correlation():
    """Matching request_id to the command is how a result is interpreted at all."""
    result = factories.make_result(request_id="student-offer-1", object_id="offer-1")
    message = bazaar_pb2.ServerMessage()
    message.result.CopyFrom(mappers.result_to_pb(result, RUN))

    decoded = decode_server_message(message.SerializeToString())

    assert decoded.request_id == "student-offer-1"
    assert decoded.object_id == "offer-1"
