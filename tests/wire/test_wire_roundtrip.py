"""Every message survives domain -> pb -> bytes -> pb -> domain unchanged.

The schema's stated traps get their own tests: required fields preserve zeros,
`false` and empty lists, and nullable wrappers must select exactly one arm.
"""

from __future__ import annotations

import bazaar_pb2
import pytest

from bazaar_client.codec.wire import decode_server_message
from bazaar_client.domain import mappers
from bazaar_client.domain.mappers import WireFormatError
from bazaar_client.domain.types import (
    Bundle,
    ControlCode,
    OfferStatus,
    Phase,
    PublicationStatus,
    Resource,
    ResultCode,
)
from tests.fixtures import factories


def roundtrip(value, to_pb, from_pb):
    """Serialize to bytes and back, so encoding is exercised, not just conversion."""
    pb = to_pb(value)
    revived = type(pb)()
    revived.ParseFromString(pb.SerializeToString())
    return from_pb(revived)


def test_bundle_roundtrip():
    bundle = Bundle(7, 0, 12)
    assert roundtrip(bundle, mappers.bundle_to_pb, mappers.bundle_from_pb) == bundle


def test_zero_bundle_keeps_all_three_fields_on_the_wire():
    """A zero is a value, not an omission: all three fields stay present."""
    pb = mappers.bundle_to_pb(Bundle.zero())
    revived = bazaar_pb2.Bundle()
    revived.ParseFromString(pb.SerializeToString())

    assert revived.HasField("water")
    assert revived.HasField("food")
    assert revived.HasField("components")
    assert mappers.bundle_from_pb(revived) == Bundle.zero()


def test_rules_roundtrip():
    rules = factories.make_rules()
    assert roundtrip(rules, mappers.rules_to_pb, mappers.rules_from_pb) == rules


def test_station_observation_roundtrip():
    station = factories.make_station(
        health=55,
        failed_once=True,
        first_failure_tick=9,
        last_production=Bundle(water=3),
        last_unmet_upkeep=Bundle(food=1),
        specialty=Resource.COMPONENTS,
    )
    assert (
        roundtrip(
            station,
            mappers.station_observation_to_pb,
            mappers.station_observation_from_pb,
        )
        == station
    )


@pytest.mark.parametrize("status", list(OfferStatus))
def test_offer_roundtrip_for_every_status(status):
    offer = factories.make_offer(
        status=status, closed_tick=4, transaction_id="txn-9"
    )
    assert roundtrip(offer, mappers.offer_to_pb, mappers.offer_from_pb) == offer


def test_offer_roundtrip_with_null_arms():
    offer = factories.make_offer(closed_tick=None, transaction_id=None)
    revived = roundtrip(offer, mappers.offer_to_pb, mappers.offer_from_pb)

    assert revived == offer
    assert revived.closed_tick is None
    assert revived.transaction_id is None


def test_transaction_roundtrip():
    txn = factories.make_transaction()
    assert roundtrip(txn, mappers.transaction_to_pb, mappers.transaction_from_pb) == txn


@pytest.mark.parametrize("status", list(PublicationStatus))
def test_advertisement_roundtrip_for_every_status(status):
    ad = factories.make_advertisement(status=status)
    assert (
        roundtrip(ad, mappers.advertisement_to_pb, mappers.advertisement_from_pb) == ad
    )


def test_advertisement_with_empty_selling_keeps_the_list_present():
    """`selling {}` is an empty list; omitting `selling` would be invalid."""
    ad = factories.make_advertisement(
        selling=frozenset(), seeking=frozenset({Resource.COMPONENTS})
    )
    pb = mappers.advertisement_to_pb(ad)
    revived = bazaar_pb2.Advertisement()
    revived.ParseFromString(pb.SerializeToString())

    assert revived.HasField("selling")
    assert list(revived.selling.items) == []
    assert mappers.advertisement_from_pb(revived) == ad


@pytest.mark.parametrize("code", list(ResultCode))
def test_result_roundtrip_for_every_code(code):
    result = factories.make_result(code=code, ok=(code is ResultCode.OK))
    revived = roundtrip(
        result,
        lambda r: mappers.result_to_pb(r, factories.RUN_ID),
        mappers.result_from_pb,
    )
    assert revived == result


def test_result_preserves_false_and_null_arms():
    """`ok: false` and an absent object_id must survive, not silently vanish."""
    result = factories.make_result(
        ok=False,
        code=ResultCode.INSUFFICIENT_RESOURCES,
        object_id=None,
        transaction_id=None,
        retry_after_tick=None,
    )
    revived = roundtrip(
        result,
        lambda r: mappers.result_to_pb(r, factories.RUN_ID),
        mappers.result_from_pb,
    )

    assert revived.ok is False
    assert revived.object_id is None
    assert revived.retry_after_tick is None


def test_result_retry_after_tick_zero_is_a_value_not_absence():
    result = factories.make_result(code=ResultCode.RATE_LIMITED, retry_after_tick=0)
    revived = roundtrip(
        result,
        lambda r: mappers.result_to_pb(r, factories.RUN_ID),
        mappers.result_from_pb,
    )
    assert revived.retry_after_tick == 0


@pytest.mark.parametrize("code", list(ControlCode))
def test_protocol_error_roundtrip_for_every_code(code):
    event = factories.make_protocol_error(code=code)
    assert (
        roundtrip(
            event, mappers.protocol_error_to_pb, mappers.protocol_error_from_pb
        )
        == event
    )


def test_protocol_error_with_absent_ids():
    event = factories.make_protocol_error(run_id=None, request_id=None, close_session=True)
    revived = roundtrip(
        event, mappers.protocol_error_to_pb, mappers.protocol_error_from_pb
    )

    assert revived.run_id is None
    assert revived.request_id is None
    assert revived.close_session is True


def test_readiness_roundtrip():
    ack = factories.make_readiness()
    assert roundtrip(ack, mappers.readiness_to_pb, mappers.readiness_from_pb) == ack


def test_readiness_false_survives():
    ack = factories.make_readiness(ready=False)
    assert roundtrip(ack, mappers.readiness_to_pb, mappers.readiness_from_pb).ready is False


@pytest.mark.parametrize("collective", [None, True, False])
def test_player_outcome_roundtrip_across_nullable_bool_arms(collective):
    outcome = factories.make_outcome(collective_success=collective)
    revived = roundtrip(
        outcome, mappers.player_outcome_to_pb, mappers.player_outcome_from_pb
    )
    assert revived.collective_success is collective


@pytest.mark.parametrize("phase", list(Phase))
def test_snapshot_roundtrip_for_every_phase(phase):
    snapshot = factories.make_snapshot(phase=phase)
    assert (
        roundtrip(snapshot, mappers.snapshot_to_pb, mappers.snapshot_from_pb) == snapshot
    )


def test_fully_populated_snapshot_roundtrip():
    """The realistic case: directory, offers, ads, transactions and results together."""
    snapshot = factories.make_snapshot(
        snapshot_sequence=9,
        world_version=9,
        offers=(
            factories.make_offer(status=OfferStatus.ACCEPTED, transaction_id="txn-1", closed_tick=0),
            factories.make_offer(
                offer_id="offer-2",
                proposer_id=factories.PEER,
                recipient_id=factories.STATION,
                give=Bundle(components=1),
                receive=Bundle.zero(),
            ),
        ),
        advertisements=(factories.make_advertisement(),),
        transactions=(factories.make_transaction(),),
        request_results=(factories.make_result(), factories.make_result(request_id="r2")),
        outcome=factories.make_outcome(collective_success=True),
    )
    revived = roundtrip(snapshot, mappers.snapshot_to_pb, mappers.snapshot_from_pb)

    assert revived == snapshot
    assert len(revived.offers) == 2
    assert len(revived.request_results) == 2


def test_snapshot_decodes_through_the_full_server_message_path():
    """The path the client actually uses: raw bytes -> domain event."""
    snapshot = factories.make_snapshot()
    message = bazaar_pb2.ServerMessage()
    message.state.CopyFrom(mappers.snapshot_to_pb(snapshot))

    assert decode_server_message(message.SerializeToString()) == snapshot


def test_null_false_is_rejected_rather_than_read_as_absent():
    """The schema rejects `null: false`; reading it as "absent" would hide a bug."""
    pb = bazaar_pb2.NullableString()
    pb.null = False

    with pytest.raises(WireFormatError, match="null: false"):
        mappers.nullable_str_from_pb(pb)


def test_nullable_with_no_arm_selected_is_rejected():
    with pytest.raises(WireFormatError, match="no arm"):
        mappers.nullable_uint_from_pb(bazaar_pb2.NullableUint())


@pytest.mark.parametrize(
    "enum_type", [Resource, Phase, OfferStatus, PublicationStatus, ResultCode, ControlCode]
)
def test_unknown_enum_value_is_surfaced_not_coerced(enum_type):
    """A value the schema does not define must raise, not become a silent default."""
    with pytest.raises(ValueError):
        enum_type(99)


def test_enum_values_match_the_schema_numbering():
    """Guards against a renumbering drifting apart from bazaar.proto."""
    assert (Resource.WATER, Resource.FOOD, Resource.COMPONENTS) == (1, 2, 3)
    assert ResultCode.STATION_FAILED == 11
    assert ControlCode.SESSION_FENCED == 6
    assert Phase.RUNNING == 2
