"""Translation between generated protobuf messages and domain types.

Together with `bazaar_client.codec.wire`, this is the only place allowed to
import `bazaar_pb2`. Three schema rules drive everything here:

* Required fields preserve zeros, `false` and empty lists, so every field is set
  explicitly and submessages are assigned with `CopyFrom` (which marks presence
  even when the source is empty).
* A nullable wrapper must select exactly one arm, and the absent arm is always
  `null: true` -- `null: false` is rejected by the server.
* Unknown enum values are a protocol error rather than something to coerce.
"""

from __future__ import annotations

from typing import Iterable

import bazaar_pb2

from bazaar_client.domain.types import (
    Advertisement,
    Bundle,
    CommandResult,
    ControlCode,
    DirectoryEntry,
    Offer,
    OfferStatus,
    Phase,
    PlayerOutcome,
    ProtocolErrorEvent,
    PublicationStatus,
    ReadinessAck,
    Resource,
    ResultCode,
    Rules,
    Snapshot,
    StationObservation,
    Transaction,
)

PROTOCOL_VERSION = "2.0"


class WireFormatError(ValueError):
    """Raised when a decoded message violates the schema's stated guarantees."""


# --- scalars and wrappers -------------------------------------------------


def bundle_from_pb(pb: bazaar_pb2.Bundle) -> Bundle:
    return Bundle(water=pb.water, food=pb.food, components=pb.components)


def bundle_to_pb(bundle: Bundle) -> bazaar_pb2.Bundle:
    return bazaar_pb2.Bundle(
        water=bundle.water, food=bundle.food, components=bundle.components
    )


def resource_list_from_pb(pb: bazaar_pb2.ListResource) -> frozenset[Resource]:
    return frozenset(Resource(item) for item in pb.items)


def resource_list_to_pb(resources: Iterable[Resource]) -> bazaar_pb2.ListResource:
    # Sorted for deterministic bytes; an empty iterable yields a present, empty list.
    return bazaar_pb2.ListResource(items=sorted(int(r) for r in resources))


def nullable_str_from_pb(pb: bazaar_pb2.NullableString) -> str | None:
    arm = pb.WhichOneof("kind")
    if arm == "value":
        return pb.value
    if arm == "null":
        if not pb.null:
            raise WireFormatError("NullableString set null: false; exactly one arm must be selected")
        return None
    raise WireFormatError("NullableString selected no arm")


def nullable_str_to_pb(value: str | None) -> bazaar_pb2.NullableString:
    if value is None:
        return bazaar_pb2.NullableString(null=True)
    return bazaar_pb2.NullableString(value=value)


def nullable_uint_from_pb(pb: bazaar_pb2.NullableUint) -> int | None:
    arm = pb.WhichOneof("kind")
    if arm == "value":
        return pb.value
    if arm == "null":
        if not pb.null:
            raise WireFormatError("NullableUint set null: false; exactly one arm must be selected")
        return None
    raise WireFormatError("NullableUint selected no arm")


def nullable_uint_to_pb(value: int | None) -> bazaar_pb2.NullableUint:
    if value is None:
        return bazaar_pb2.NullableUint(null=True)
    return bazaar_pb2.NullableUint(value=value)


def nullable_bool_from_pb(pb: bazaar_pb2.NullableBool) -> bool | None:
    arm = pb.WhichOneof("kind")
    if arm == "value":
        return pb.value
    if arm == "null":
        if not pb.null:
            raise WireFormatError("NullableBool set null: false; exactly one arm must be selected")
        return None
    raise WireFormatError("NullableBool selected no arm")


def nullable_bool_to_pb(value: bool | None) -> bazaar_pb2.NullableBool:
    if value is None:
        return bazaar_pb2.NullableBool(null=True)
    return bazaar_pb2.NullableBool(value=value)


# --- server -> domain -----------------------------------------------------


def rules_from_pb(pb: bazaar_pb2.PublicRules) -> Rules:
    return Rules(
        rules_version=pb.rules_version,
        duration_ticks=pb.duration_ticks,
        tick_duration_ms=pb.tick_duration_ms,
        resource_order=tuple(Resource(item) for item in pb.resource_order.items),
        max_health=pb.max_health,
        shortage_damage_per_unit=pb.shortage_damage_per_unit,
        recovery_per_fully_supplied_tick=pb.recovery_per_fully_supplied_tick,
        max_publication_ttl_ticks=pb.max_publication_ttl_ticks,
        max_offer_ttl_ticks=pb.max_offer_ttl_ticks,
        new_commands_per_station_per_tick=pb.new_commands_per_station_per_tick,
        max_request_records_per_station=pb.max_request_records_per_station,
        max_open_outgoing_offers=pb.max_open_outgoing_offers,
        max_command_bytes=pb.max_command_bytes,
    )


def rules_to_pb(rules: Rules) -> bazaar_pb2.PublicRules:
    pb = bazaar_pb2.PublicRules(
        rules_version=rules.rules_version,
        duration_ticks=rules.duration_ticks,
        tick_duration_ms=rules.tick_duration_ms,
        max_health=rules.max_health,
        shortage_damage_per_unit=rules.shortage_damage_per_unit,
        recovery_per_fully_supplied_tick=rules.recovery_per_fully_supplied_tick,
        max_publication_ttl_ticks=rules.max_publication_ttl_ticks,
        max_offer_ttl_ticks=rules.max_offer_ttl_ticks,
        new_commands_per_station_per_tick=rules.new_commands_per_station_per_tick,
        max_request_records_per_station=rules.max_request_records_per_station,
        max_open_outgoing_offers=rules.max_open_outgoing_offers,
        max_command_bytes=rules.max_command_bytes,
    )
    pb.resource_order.CopyFrom(resource_list_to_pb(rules.resource_order))
    return pb


def station_observation_from_pb(pb: bazaar_pb2.StationObservation) -> StationObservation:
    return StationObservation(
        station_id=pb.station_id,
        inventory=bundle_from_pb(pb.inventory),
        health=pb.health,
        failed_once=pb.failed_once,
        first_failure_tick=nullable_uint_from_pb(pb.first_failure_tick),
        last_production=bundle_from_pb(pb.last_production),
        last_unmet_upkeep=bundle_from_pb(pb.last_unmet_upkeep),
        fully_supplied_ticks=pb.fully_supplied_ticks,
        shortage_ticks=pb.shortage_ticks,
        current_shortage_streak=pb.current_shortage_streak,
        longest_shortage_streak=pb.longest_shortage_streak,
        produced_total=bundle_from_pb(pb.produced_total),
        consumed_total=bundle_from_pb(pb.consumed_total),
        unmet_total=bundle_from_pb(pb.unmet_total),
        imported_total=bundle_from_pb(pb.imported_total),
        exported_total=bundle_from_pb(pb.exported_total),
        upkeep_per_tick=bundle_from_pb(pb.upkeep_per_tick),
        specialty=Resource(pb.specialty),
    )


def station_observation_to_pb(obs: StationObservation) -> bazaar_pb2.StationObservation:
    pb = bazaar_pb2.StationObservation(
        station_id=obs.station_id,
        health=obs.health,
        failed_once=obs.failed_once,
        fully_supplied_ticks=obs.fully_supplied_ticks,
        shortage_ticks=obs.shortage_ticks,
        current_shortage_streak=obs.current_shortage_streak,
        longest_shortage_streak=obs.longest_shortage_streak,
        specialty=int(obs.specialty),
    )
    pb.inventory.CopyFrom(bundle_to_pb(obs.inventory))
    pb.first_failure_tick.CopyFrom(nullable_uint_to_pb(obs.first_failure_tick))
    pb.last_production.CopyFrom(bundle_to_pb(obs.last_production))
    pb.last_unmet_upkeep.CopyFrom(bundle_to_pb(obs.last_unmet_upkeep))
    pb.produced_total.CopyFrom(bundle_to_pb(obs.produced_total))
    pb.consumed_total.CopyFrom(bundle_to_pb(obs.consumed_total))
    pb.unmet_total.CopyFrom(bundle_to_pb(obs.unmet_total))
    pb.imported_total.CopyFrom(bundle_to_pb(obs.imported_total))
    pb.exported_total.CopyFrom(bundle_to_pb(obs.exported_total))
    pb.upkeep_per_tick.CopyFrom(bundle_to_pb(obs.upkeep_per_tick))
    return pb


def directory_entry_from_pb(pb: bazaar_pb2.DirectoryEntry) -> DirectoryEntry:
    return DirectoryEntry(station_id=pb.station_id, display_name=pb.display_name)


def directory_entry_to_pb(entry: DirectoryEntry) -> bazaar_pb2.DirectoryEntry:
    return bazaar_pb2.DirectoryEntry(
        station_id=entry.station_id, display_name=entry.display_name
    )


def offer_from_pb(pb: bazaar_pb2.Offer) -> Offer:
    return Offer(
        offer_id=pb.offer_id,
        proposer_id=pb.proposer_id,
        recipient_id=pb.recipient_id,
        give=bundle_from_pb(pb.give),
        receive=bundle_from_pb(pb.receive),
        created_tick=pb.created_tick,
        created_version=pb.created_version,
        expires_tick=pb.expires_tick,
        status=OfferStatus(pb.status),
        closed_tick=nullable_uint_from_pb(pb.closed_tick),
        transaction_id=nullable_str_from_pb(pb.transaction_id),
    )


def offer_to_pb(offer: Offer) -> bazaar_pb2.Offer:
    pb = bazaar_pb2.Offer(
        offer_id=offer.offer_id,
        proposer_id=offer.proposer_id,
        recipient_id=offer.recipient_id,
        created_tick=offer.created_tick,
        created_version=offer.created_version,
        expires_tick=offer.expires_tick,
        status=int(offer.status),
    )
    pb.give.CopyFrom(bundle_to_pb(offer.give))
    pb.receive.CopyFrom(bundle_to_pb(offer.receive))
    pb.closed_tick.CopyFrom(nullable_uint_to_pb(offer.closed_tick))
    pb.transaction_id.CopyFrom(nullable_str_to_pb(offer.transaction_id))
    return pb


def transaction_from_pb(pb: bazaar_pb2.Transaction) -> Transaction:
    return Transaction(
        transaction_id=pb.transaction_id,
        offer_id=pb.offer_id,
        proposer_id=pb.proposer_id,
        recipient_id=pb.recipient_id,
        give=bundle_from_pb(pb.give),
        receive=bundle_from_pb(pb.receive),
        settled_tick=pb.settled_tick,
        settled_version=pb.settled_version,
    )


def transaction_to_pb(txn: Transaction) -> bazaar_pb2.Transaction:
    pb = bazaar_pb2.Transaction(
        transaction_id=txn.transaction_id,
        offer_id=txn.offer_id,
        proposer_id=txn.proposer_id,
        recipient_id=txn.recipient_id,
        settled_tick=txn.settled_tick,
        settled_version=txn.settled_version,
    )
    pb.give.CopyFrom(bundle_to_pb(txn.give))
    pb.receive.CopyFrom(bundle_to_pb(txn.receive))
    return pb


def advertisement_from_pb(pb: bazaar_pb2.Advertisement) -> Advertisement:
    return Advertisement(
        advertisement_id=pb.advertisement_id,
        station_id=pb.station_id,
        selling=resource_list_from_pb(pb.selling),
        seeking=resource_list_from_pb(pb.seeking),
        created_tick=pb.created_tick,
        expires_tick=pb.expires_tick,
        created_version=pb.created_version,
        status=PublicationStatus(pb.status),
    )


def advertisement_to_pb(ad: Advertisement) -> bazaar_pb2.Advertisement:
    pb = bazaar_pb2.Advertisement(
        advertisement_id=ad.advertisement_id,
        station_id=ad.station_id,
        created_tick=ad.created_tick,
        expires_tick=ad.expires_tick,
        created_version=ad.created_version,
        status=int(ad.status),
    )
    pb.selling.CopyFrom(resource_list_to_pb(ad.selling))
    pb.seeking.CopyFrom(resource_list_to_pb(ad.seeking))
    return pb


def result_from_pb(pb: bazaar_pb2.Result) -> CommandResult:
    return CommandResult(
        request_id=pb.request_id,
        ok=pb.ok,
        code=ResultCode(pb.code),
        processed_tick=pb.processed_tick,
        processed_version=pb.processed_version,
        object_id=nullable_str_from_pb(pb.object_id),
        transaction_id=nullable_str_from_pb(pb.transaction_id),
        retry_after_tick=nullable_uint_from_pb(pb.retry_after_tick),
    )


def result_to_pb(result: CommandResult, run_id: str) -> bazaar_pb2.Result:
    pb = bazaar_pb2.Result(
        type=bazaar_pb2.RESULT_TYPE_RESULT,
        protocol_version=PROTOCOL_VERSION,
        run_id=run_id,
        request_id=result.request_id,
        ok=result.ok,
        code=int(result.code),
        processed_tick=result.processed_tick,
        processed_version=result.processed_version,
    )
    pb.object_id.CopyFrom(nullable_str_to_pb(result.object_id))
    pb.transaction_id.CopyFrom(nullable_str_to_pb(result.transaction_id))
    pb.retry_after_tick.CopyFrom(nullable_uint_to_pb(result.retry_after_tick))
    return pb


def protocol_error_from_pb(pb: bazaar_pb2.ProtocolError) -> ProtocolErrorEvent:
    return ProtocolErrorEvent(
        run_id=nullable_str_from_pb(pb.run_id),
        request_id=nullable_str_from_pb(pb.request_id),
        code=ControlCode(pb.code),
        close_session=pb.close_session,
    )


def protocol_error_to_pb(event: ProtocolErrorEvent) -> bazaar_pb2.ProtocolError:
    pb = bazaar_pb2.ProtocolError(
        type=bazaar_pb2.PROTOCOL_ERROR_TYPE_PROTOCOL_ERROR,
        protocol_version=PROTOCOL_VERSION,
        code=int(event.code),
        close_session=event.close_session,
    )
    pb.run_id.CopyFrom(nullable_str_to_pb(event.run_id))
    pb.request_id.CopyFrom(nullable_str_to_pb(event.request_id))
    return pb


def readiness_from_pb(pb: bazaar_pb2.Readiness) -> ReadinessAck:
    return ReadinessAck(
        run_id=pb.run_id, ready=pb.ready, snapshot_sequence=pb.snapshot_sequence
    )


def readiness_to_pb(ack: ReadinessAck) -> bazaar_pb2.Readiness:
    return bazaar_pb2.Readiness(
        type=bazaar_pb2.READINESS_TYPE_READINESS,
        protocol_version=PROTOCOL_VERSION,
        run_id=ack.run_id,
        ready=ack.ready,
        snapshot_sequence=ack.snapshot_sequence,
    )


def player_outcome_from_pb(pb: bazaar_pb2.PlayerOutcome) -> PlayerOutcome:
    return PlayerOutcome(
        collective_success=nullable_bool_from_pb(pb.collective_success),
        self_failed=pb.self_failed,
        aborted=pb.aborted,
    )


def player_outcome_to_pb(outcome: PlayerOutcome) -> bazaar_pb2.PlayerOutcome:
    pb = bazaar_pb2.PlayerOutcome(
        self_failed=outcome.self_failed, aborted=outcome.aborted
    )
    pb.collective_success.CopyFrom(nullable_bool_to_pb(outcome.collective_success))
    return pb


def nullable_outcome_from_pb(
    pb: bazaar_pb2.NullablePlayerOutcome,
) -> PlayerOutcome | None:
    arm = pb.WhichOneof("kind")
    if arm == "value":
        return player_outcome_from_pb(pb.value)
    if arm == "null":
        if not pb.null:
            raise WireFormatError(
                "NullablePlayerOutcome set null: false; exactly one arm must be selected"
            )
        return None
    raise WireFormatError("NullablePlayerOutcome selected no arm")


def nullable_outcome_to_pb(
    outcome: PlayerOutcome | None,
) -> bazaar_pb2.NullablePlayerOutcome:
    if outcome is None:
        return bazaar_pb2.NullablePlayerOutcome(null=True)
    pb = bazaar_pb2.NullablePlayerOutcome()
    pb.value.CopyFrom(player_outcome_to_pb(outcome))
    return pb


def snapshot_from_pb(pb: bazaar_pb2.State) -> Snapshot:
    return Snapshot(
        run_id=pb.run_id,
        snapshot_sequence=pb.snapshot_sequence,
        world_version=pb.world_version,
        tick=pb.tick,
        phase=Phase(pb.phase),
        self_station_id=pb.self_station_id,
        rules=rules_from_pb(pb.rules),
        directory=tuple(directory_entry_from_pb(e) for e in pb.directory.items),
        me=station_observation_from_pb(getattr(pb, "self")),
        offers=tuple(offer_from_pb(o) for o in pb.offers.items),
        advertisements=tuple(advertisement_from_pb(a) for a in pb.advertisements.items),
        transactions=tuple(transaction_from_pb(t) for t in pb.transactions.items),
        request_results=tuple(result_from_pb(r) for r in pb.request_results.items),
        outcome=nullable_outcome_from_pb(pb.outcome),
    )


def snapshot_to_pb(snapshot: Snapshot) -> bazaar_pb2.State:
    """Build a State message. Used by tests and fixtures; the client never sends one."""
    pb = bazaar_pb2.State(
        type=bazaar_pb2.STATE_TYPE_STATE,
        protocol_version=PROTOCOL_VERSION,
        run_id=snapshot.run_id,
        snapshot_sequence=snapshot.snapshot_sequence,
        world_version=snapshot.world_version,
        tick=snapshot.tick,
        phase=int(snapshot.phase),
        self_station_id=snapshot.self_station_id,
    )
    pb.rules.CopyFrom(rules_to_pb(snapshot.rules))
    pb.directory.CopyFrom(
        bazaar_pb2.ListDirectoryEntry(
            items=[directory_entry_to_pb(e) for e in snapshot.directory]
        )
    )
    getattr(pb, "self").CopyFrom(station_observation_to_pb(snapshot.me))
    pb.offers.CopyFrom(
        bazaar_pb2.ListOffer(items=[offer_to_pb(o) for o in snapshot.offers])
    )
    pb.advertisements.CopyFrom(
        bazaar_pb2.ListAdvertisement(
            items=[advertisement_to_pb(a) for a in snapshot.advertisements]
        )
    )
    pb.transactions.CopyFrom(
        bazaar_pb2.ListTransaction(
            items=[transaction_to_pb(t) for t in snapshot.transactions]
        )
    )
    pb.request_results.CopyFrom(
        bazaar_pb2.ListResult(
            items=[result_to_pb(r, snapshot.run_id) for r in snapshot.request_results]
        )
    )
    pb.outcome.CopyFrom(nullable_outcome_to_pb(snapshot.outcome))
    return pb


# --- domain -> client commands -------------------------------------------


def build_advertise(
    run_id: str,
    request_id: str,
    selling: Iterable[Resource],
    seeking: Iterable[Resource],
    expires_tick: int,
) -> bazaar_pb2.ClientMessage:
    body = bazaar_pb2.AdvertiseBody(expires_tick=expires_tick)
    body.selling.CopyFrom(resource_list_to_pb(selling))
    body.seeking.CopyFrom(resource_list_to_pb(seeking))
    msg = bazaar_pb2.ClientMessage()
    msg.advertise.type = bazaar_pb2.ADVERTISE_TYPE_ADVERTISE
    msg.advertise.protocol_version = PROTOCOL_VERSION
    msg.advertise.run_id = run_id
    msg.advertise.request_id = request_id
    msg.advertise.body.CopyFrom(body)
    return msg


def build_offer(
    run_id: str,
    request_id: str,
    recipient_id: str,
    give: Bundle,
    receive: Bundle,
    expires_tick: int,
) -> bazaar_pb2.ClientMessage:
    body = bazaar_pb2.OfferBody(recipient_id=recipient_id, expires_tick=expires_tick)
    body.give.CopyFrom(bundle_to_pb(give))
    body.receive.CopyFrom(bundle_to_pb(receive))
    msg = bazaar_pb2.ClientMessage()
    msg.offer.type = bazaar_pb2.OFFER_COMMAND_TYPE_OFFER
    msg.offer.protocol_version = PROTOCOL_VERSION
    msg.offer.run_id = run_id
    msg.offer.request_id = request_id
    msg.offer.body.CopyFrom(body)
    return msg


def build_accept(run_id: str, request_id: str, offer_id: str) -> bazaar_pb2.ClientMessage:
    msg = bazaar_pb2.ClientMessage()
    msg.accept.type = bazaar_pb2.ACCEPT_TYPE_ACCEPT
    msg.accept.protocol_version = PROTOCOL_VERSION
    msg.accept.run_id = run_id
    msg.accept.request_id = request_id
    msg.accept.body.CopyFrom(bazaar_pb2.AcceptBody(offer_id=offer_id))
    return msg


def build_withdraw(
    run_id: str, request_id: str, object_id: str
) -> bazaar_pb2.ClientMessage:
    msg = bazaar_pb2.ClientMessage()
    msg.withdraw.type = bazaar_pb2.WITHDRAW_TYPE_WITHDRAW
    msg.withdraw.protocol_version = PROTOCOL_VERSION
    msg.withdraw.run_id = run_id
    msg.withdraw.request_id = request_id
    msg.withdraw.body.CopyFrom(bazaar_pb2.WithdrawBody(object_id=object_id))
    return msg


def build_sync(run_id: str) -> bazaar_pb2.ClientMessage:
    """Sync carries no body and no request_id."""
    msg = bazaar_pb2.ClientMessage()
    msg.sync.type = bazaar_pb2.SYNC_TYPE_SYNC
    msg.sync.protocol_version = PROTOCOL_VERSION
    msg.sync.run_id = run_id
    return msg


def build_ready(
    run_id: str, ready: bool, snapshot_sequence: int
) -> bazaar_pb2.ClientMessage:
    msg = bazaar_pb2.ClientMessage()
    msg.ready.type = bazaar_pb2.READY_TYPE_READY
    msg.ready.protocol_version = PROTOCOL_VERSION
    msg.ready.run_id = run_id
    msg.ready.ready = ready
    msg.ready.snapshot_sequence = snapshot_sequence
    return msg
