"""Domain types for the Spaceport Bazaar client.

These mirror `bazaar.proto` but are plain immutable Python objects. Nothing in
this module imports protobuf: translation lives in `bazaar_client.domain.mappers`.

Naming note: the proto field `State.self` is exposed here as `Snapshot.me`,
since `self` would shadow the method receiver everywhere it is read.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class NegativeBundleError(ValueError):
    """Raised when bundle subtraction would produce a negative quantity."""


class Resource(enum.IntEnum):
    WATER = 1
    FOOD = 2
    COMPONENTS = 3


class Phase(enum.IntEnum):
    READY = 1
    RUNNING = 2
    PAUSED = 3
    FINISHED = 4
    ABORTED = 5


class OfferStatus(enum.IntEnum):
    OPEN = 1
    ACCEPTED = 2
    WITHDRAWN = 3
    EXPIRED = 4
    RUN_ENDED = 5


class PublicationStatus(enum.IntEnum):
    ACTIVE = 1
    REPLACED = 2
    WITHDRAWN = 3
    EXPIRED = 4
    RUN_ENDED = 5


class ResultCode(enum.IntEnum):
    OK = 1
    REQUEST_ID_CONFLICT = 2
    RUN_NOT_RUNNING = 3
    RATE_LIMITED = 4
    INVALID_ARGUMENT = 5
    NOT_FOUND = 6
    EXPIRED = 7
    NOT_OPEN = 8
    LIMIT_REACHED = 9
    INSUFFICIENT_RESOURCES = 10
    STATION_FAILED = 11


class ControlCode(enum.IntEnum):
    BAD_MESSAGE = 1
    REQUEST_CAPACITY_EXCEEDED = 2
    UNSUPPORTED_VERSION = 3
    RUN_MISMATCH = 4
    INVALID_AUTHENTICATION = 5
    SESSION_FENCED = 6


_RESOURCE_ATTRS: dict[Resource, str] = {
    Resource.WATER: "water",
    Resource.FOOD: "food",
    Resource.COMPONENTS: "components",
}


@dataclass(frozen=True, slots=True)
class Bundle:
    """A quantity of each of the three resources. Always all three, including zeros."""

    water: int = 0
    food: int = 0
    components: int = 0

    def __post_init__(self) -> None:
        for resource in Resource:
            if self.get(resource) < 0:
                raise NegativeBundleError(f"{resource.name} quantity is negative: {self.get(resource)}")

    @classmethod
    def zero(cls) -> Bundle:
        return cls(0, 0, 0)

    @classmethod
    def single(cls, resource: Resource, quantity: int) -> Bundle:
        return cls(**{_RESOURCE_ATTRS[resource]: quantity})

    def get(self, resource: Resource) -> int:
        return getattr(self, _RESOURCE_ATTRS[resource])

    def __add__(self, other: Bundle) -> Bundle:
        return Bundle(*(self.get(r) + other.get(r) for r in Resource))

    def __sub__(self, other: Bundle) -> Bundle:
        """Strict subtraction. Raises NegativeBundleError rather than going negative."""
        return Bundle(*(self.get(r) - other.get(r) for r in Resource))

    def saturating_sub(self, other: Bundle) -> Bundle:
        return Bundle(*(max(0, self.get(r) - other.get(r)) for r in Resource))

    def scale(self, factor: int) -> Bundle:
        return Bundle(*(self.get(r) * factor for r in Resource))

    def dominates(self, other: Bundle) -> bool:
        """True when this bundle covers `other` in every resource."""
        return all(self.get(r) >= other.get(r) for r in Resource)

    def is_zero(self) -> bool:
        return all(self.get(r) == 0 for r in Resource)

    def total(self) -> int:
        return sum(self.get(r) for r in Resource)

    def as_dict(self) -> dict[str, int]:
        return {"water": self.water, "food": self.food, "components": self.components}


@dataclass(frozen=True, slots=True)
class Rules:
    """Server-published limits. Read at runtime; never hardcode these numbers."""

    rules_version: str
    duration_ticks: int
    tick_duration_ms: int
    resource_order: tuple[Resource, ...]
    max_health: int
    shortage_damage_per_unit: int
    recovery_per_fully_supplied_tick: int
    max_publication_ttl_ticks: int
    max_offer_ttl_ticks: int
    new_commands_per_station_per_tick: int
    max_request_records_per_station: int
    max_open_outgoing_offers: int
    max_command_bytes: int


@dataclass(frozen=True, slots=True)
class StationObservation:
    """Everything visible about our own station. Never available for other stations."""

    station_id: str
    inventory: Bundle
    health: int
    failed_once: bool
    first_failure_tick: int | None
    last_production: Bundle
    last_unmet_upkeep: Bundle
    fully_supplied_ticks: int
    shortage_ticks: int
    current_shortage_streak: int
    longest_shortage_streak: int
    produced_total: Bundle
    consumed_total: Bundle
    unmet_total: Bundle
    imported_total: Bundle
    exported_total: Bundle
    upkeep_per_tick: Bundle
    specialty: Resource


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    station_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class Offer:
    """A proposed exchange. `give`/`receive` are always the PROPOSER's perspective."""

    offer_id: str
    proposer_id: str
    recipient_id: str
    give: Bundle
    receive: Bundle
    created_tick: int
    created_version: int
    expires_tick: int
    status: OfferStatus
    closed_tick: int | None
    transaction_id: str | None

    def is_expired_at(self, tick: int) -> bool:
        """Deadlines are exclusive: expires_tick 12 means unusable from tick 12 on."""
        return tick >= self.expires_tick

    def is_open_at(self, tick: int) -> bool:
        return self.status is OfferStatus.OPEN and not self.is_expired_at(tick)

    def what_station_pays(self, station_id: str) -> Bundle:
        """Resolve proposer-perspective terms into what `station_id` hands over."""
        if station_id == self.proposer_id:
            return self.give
        if station_id == self.recipient_id:
            return self.receive
        raise ValueError(f"{station_id} is not a party to offer {self.offer_id}")

    def what_station_receives(self, station_id: str) -> Bundle:
        if station_id == self.proposer_id:
            return self.receive
        if station_id == self.recipient_id:
            return self.give
        raise ValueError(f"{station_id} is not a party to offer {self.offer_id}")

    def is_gift_to(self, station_id: str) -> bool:
        return station_id == self.recipient_id and self.receive.is_zero()


@dataclass(frozen=True, slots=True)
class Transaction:
    """A settled exchange. Terms stay in the proposer's perspective."""

    transaction_id: str
    offer_id: str
    proposer_id: str
    recipient_id: str
    give: Bundle
    receive: Bundle
    settled_tick: int
    settled_version: int


@dataclass(frozen=True, slots=True)
class Advertisement:
    """A public claim to sell or seek. Proves nothing about actual stock."""

    advertisement_id: str
    station_id: str
    selling: frozenset[Resource]
    seeking: frozenset[Resource]
    created_tick: int
    expires_tick: int
    created_version: int
    status: PublicationStatus

    def is_expired_at(self, tick: int) -> bool:
        return tick >= self.expires_tick


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One command's outcome. `ok` and `code` together say what actually happened."""

    request_id: str
    ok: bool
    code: ResultCode
    processed_tick: int
    processed_version: int
    object_id: str | None
    transaction_id: str | None
    retry_after_tick: int | None


@dataclass(frozen=True, slots=True)
class ProtocolErrorEvent:
    """A control-level failure. Not a ResultCode; always honour close_session."""

    run_id: str | None
    request_id: str | None
    code: ControlCode
    close_session: bool


@dataclass(frozen=True, slots=True)
class ReadinessAck:
    run_id: str
    ready: bool
    snapshot_sequence: int


@dataclass(frozen=True, slots=True)
class PlayerOutcome:
    collective_success: bool | None
    self_failed: bool
    aborted: bool


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A complete view of the world at one moment. Replaces any previous view."""

    run_id: str
    snapshot_sequence: int
    world_version: int
    tick: int
    phase: Phase
    self_station_id: str
    rules: Rules
    directory: tuple[DirectoryEntry, ...]
    me: StationObservation
    offers: tuple[Offer, ...]
    advertisements: tuple[Advertisement, ...]
    transactions: tuple[Transaction, ...]
    request_results: tuple[CommandResult, ...]
    outcome: PlayerOutcome | None

    def incoming_open_offers(self) -> tuple[Offer, ...]:
        """Open offers addressed to us: the only ones we are allowed to accept."""
        return tuple(
            o
            for o in self.offers
            if o.recipient_id == self.self_station_id and o.is_open_at(self.tick)
        )

    def outgoing_open_offers(self) -> tuple[Offer, ...]:
        """Open offers we proposed: the only ones we are allowed to withdraw."""
        return tuple(
            o
            for o in self.offers
            if o.proposer_id == self.self_station_id and o.is_open_at(self.tick)
        )

    def own_advertisement(self) -> Advertisement | None:
        """Our single active advertisement, if any."""
        for ad in self.advertisements:
            if ad.station_id == self.self_station_id and ad.status is PublicationStatus.ACTIVE:
                return ad
        return None


ServerEvent = Snapshot | CommandResult | ProtocolErrorEvent | ReadinessAck
