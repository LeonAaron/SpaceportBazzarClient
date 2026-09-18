"""Builders for fully-populated domain objects.

Every required proto field has a value here, so a round-trip test exercises the
whole message rather than whatever subset a hand-written literal remembered.
"""

from __future__ import annotations

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

RUN_ID = "run-abc123"
STATION = "P01"
PEER = "P02"


def make_rules(**overrides) -> Rules:
    defaults = dict(
        rules_version="classroom-1",
        duration_ticks=120,
        tick_duration_ms=5000,
        resource_order=(Resource.WATER, Resource.FOOD, Resource.COMPONENTS),
        max_health=100,
        shortage_damage_per_unit=5,
        recovery_per_fully_supplied_tick=5,
        max_publication_ttl_ticks=10,
        max_offer_ttl_ticks=10,
        new_commands_per_station_per_tick=4,
        max_request_records_per_station=5,
        max_open_outgoing_offers=8,
        max_command_bytes=16384,
    )
    return Rules(**{**defaults, **overrides})


def make_station(**overrides) -> StationObservation:
    defaults = dict(
        station_id=STATION,
        inventory=Bundle(30, 30, 30),
        health=100,
        failed_once=False,
        first_failure_tick=None,
        last_production=Bundle.zero(),
        last_unmet_upkeep=Bundle.zero(),
        fully_supplied_ticks=0,
        shortage_ticks=0,
        current_shortage_streak=0,
        longest_shortage_streak=0,
        produced_total=Bundle.zero(),
        consumed_total=Bundle.zero(),
        unmet_total=Bundle.zero(),
        imported_total=Bundle.zero(),
        exported_total=Bundle.zero(),
        upkeep_per_tick=Bundle(1, 1, 1),
        specialty=Resource.WATER,
    )
    return StationObservation(**{**defaults, **overrides})


def make_offer(**overrides) -> Offer:
    defaults = dict(
        offer_id="offer-1",
        proposer_id=STATION,
        recipient_id=PEER,
        give=Bundle(water=2),
        receive=Bundle(food=1),
        created_tick=0,
        created_version=5,
        expires_tick=6,
        status=OfferStatus.OPEN,
        closed_tick=None,
        transaction_id=None,
    )
    return Offer(**{**defaults, **overrides})


def make_transaction(**overrides) -> Transaction:
    defaults = dict(
        transaction_id="txn-1",
        offer_id="offer-1",
        proposer_id=STATION,
        recipient_id=PEER,
        give=Bundle(water=2),
        receive=Bundle(food=1),
        settled_tick=0,
        settled_version=6,
    )
    return Transaction(**{**defaults, **overrides})


def make_advertisement(**overrides) -> Advertisement:
    defaults = dict(
        advertisement_id="ad-1",
        station_id=STATION,
        selling=frozenset({Resource.WATER}),
        seeking=frozenset({Resource.FOOD}),
        created_tick=0,
        expires_tick=6,
        created_version=3,
        status=PublicationStatus.ACTIVE,
    )
    return Advertisement(**{**defaults, **overrides})


def make_result(**overrides) -> CommandResult:
    defaults = dict(
        request_id="student-advertise-1",
        ok=True,
        code=ResultCode.OK,
        processed_tick=0,
        processed_version=3,
        object_id="ad-1",
        transaction_id=None,
        retry_after_tick=None,
    )
    return CommandResult(**{**defaults, **overrides})


def make_protocol_error(**overrides) -> ProtocolErrorEvent:
    defaults = dict(
        run_id=RUN_ID,
        request_id="student-advertise-2",
        code=ControlCode.REQUEST_CAPACITY_EXCEEDED,
        close_session=False,
    )
    return ProtocolErrorEvent(**{**defaults, **overrides})


def make_readiness(**overrides) -> ReadinessAck:
    defaults = dict(run_id=RUN_ID, ready=True, snapshot_sequence=1)
    return ReadinessAck(**{**defaults, **overrides})


def make_outcome(**overrides) -> PlayerOutcome:
    defaults = dict(collective_success=None, self_failed=False, aborted=False)
    return PlayerOutcome(**{**defaults, **overrides})


def make_snapshot(**overrides) -> Snapshot:
    defaults = dict(
        run_id=RUN_ID,
        snapshot_sequence=1,
        world_version=2,
        tick=0,
        phase=Phase.RUNNING,
        self_station_id=STATION,
        rules=make_rules(),
        directory=(
            DirectoryEntry(station_id=STATION, display_name="Aqua Prime"),
            DirectoryEntry(station_id=PEER, display_name="Verdant"),
        ),
        me=make_station(),
        offers=(),
        advertisements=(),
        transactions=(),
        request_results=(),
        outcome=None,
    )
    return Snapshot(**{**defaults, **overrides})
