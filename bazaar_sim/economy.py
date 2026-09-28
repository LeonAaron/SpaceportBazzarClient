"""The economy the field manual describes, as one authoritative rule engine.

Production, upkeep, shortage damage and recovery happen once per tick. Offers
reserve nothing when posted; an accept settles both bundles atomically or not
at all. Zero health is permanent. The same engine backs the survival tests,
`bazaar_sim.server` and `bazaar_sim.benchmark`, so a rule is written once.

Known simplifications are listed in SIMULATOR.md.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace

from bazaar_client.domain.types import (
    Advertisement,
    Bundle,
    CommandResult,
    DirectoryEntry,
    Offer,
    OfferStatus,
    Phase,
    PlayerOutcome,
    PublicationStatus,
    Resource,
    ResultCode,
    Rules,
    Snapshot,
    StationObservation,
    Transaction,
)
from bazaar_client.execution.actions import (
    AcceptAction,
    AdvertiseAction,
    OfferAction,
    WithdrawAction,
)

DEFAULT_RULES = Rules(
    rules_version="bazaar-sim-1",
    duration_ticks=120,
    tick_duration_ms=5000,
    resource_order=(Resource.WATER, Resource.FOOD, Resource.COMPONENTS),
    max_health=100,
    shortage_damage_per_unit=5,
    recovery_per_fully_supplied_tick=5,
    max_publication_ttl_ticks=10,
    max_offer_ttl_ticks=10,
    new_commands_per_station_per_tick=4,
    max_request_records_per_station=1000,
    max_open_outgoing_offers=6,
    max_command_bytes=16384,
)
DEFAULT_UPKEEP = Bundle(1, 1, 1)


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    code: ResultCode
    object_id: str | None = None
    transaction_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.code is ResultCode.OK


@dataclass
class SimStation:
    station_id: str
    specialty: Resource
    inventory: Bundle
    production: int = 3
    upkeep: Bundle = DEFAULT_UPKEEP
    display_name: str = ""
    health: int = 100
    failed_once: bool = False
    first_failure_tick: int | None = None
    shortage_ticks: int = 0
    fully_supplied_ticks: int = 0
    current_shortage_streak: int = 0
    longest_shortage_streak: int = 0
    last_production: Bundle = field(default_factory=Bundle.zero)
    last_unmet: Bundle = field(default_factory=Bundle.zero)
    produced_total: Bundle = field(default_factory=Bundle.zero)
    consumed_total: Bundle = field(default_factory=Bundle.zero)
    unmet_total: Bundle = field(default_factory=Bundle.zero)
    imported: Bundle = field(default_factory=Bundle.zero)
    exported: Bundle = field(default_factory=Bundle.zero)

    def settle_tick(self, rules: Rules, tick: int) -> None:
        """Produce, then pay upkeep; unmet upkeep costs health, a full tick restores it."""
        produced = Bundle.single(self.specialty, self.production)
        self.inventory = self.inventory + produced
        self.last_production = produced
        self.produced_total = self.produced_total + produced

        unmet = self.upkeep.saturating_sub(self.inventory)
        consumed = self.upkeep - unmet
        self.inventory = self.inventory.saturating_sub(self.upkeep)
        self.last_unmet = unmet
        self.consumed_total = self.consumed_total + consumed
        self.unmet_total = self.unmet_total + unmet

        if unmet.is_zero():
            self.health = min(rules.max_health, self.health + rules.recovery_per_fully_supplied_tick)
            self.fully_supplied_ticks += 1
            self.current_shortage_streak = 0
        else:
            self.health = max(0, self.health - unmet.total() * rules.shortage_damage_per_unit)
            self.shortage_ticks += 1
            self.current_shortage_streak += 1
            self.longest_shortage_streak = max(
                self.longest_shortage_streak, self.current_shortage_streak
            )
            if self.health == 0 and not self.failed_once:
                self.failed_once = True
                self.first_failure_tick = tick

    def observation(self) -> StationObservation:
        return StationObservation(
            station_id=self.station_id,
            inventory=self.inventory,
            health=self.health,
            failed_once=self.failed_once,
            first_failure_tick=self.first_failure_tick,
            last_production=self.last_production,
            last_unmet_upkeep=self.last_unmet,
            fully_supplied_ticks=self.fully_supplied_ticks,
            shortage_ticks=self.shortage_ticks,
            current_shortage_streak=self.current_shortage_streak,
            longest_shortage_streak=self.longest_shortage_streak,
            produced_total=self.produced_total,
            consumed_total=self.consumed_total,
            unmet_total=self.unmet_total,
            imported_total=self.imported,
            exported_total=self.exported,
            upkeep_per_tick=self.upkeep,
            specialty=self.specialty,
        )


class SimulatedEconomy:
    """A roster of planets trading under the published limits."""

    def __init__(self, *stations: SimStation, rules: Rules = DEFAULT_RULES, run_id: str = "sim") -> None:
        self.stations = {s.station_id: s for s in stations}
        self.rules = rules
        self.run_id = run_id
        self.tick = 0
        self.world_version = 1
        self.sequence = 0
        self.offers: list[Offer] = []
        self.transactions: list[Transaction] = []
        self.advertisements: list[Advertisement] = []
        self._ids = itertools.count(1)
        self.command_counts: dict[str, int] = {}
        self.rejections: list[tuple[str, str]] = []

    # --- observation ------------------------------------------------------

    def observation_for(
        self,
        station_id: str,
        *,
        sequence: int | None = None,
        phase: Phase = Phase.RUNNING,
        request_results: tuple[CommandResult, ...] = (),
        outcome: PlayerOutcome | None = None,
    ) -> Snapshot:
        """What `station_id` may see: its own station, its offers and trades, public ads."""
        if sequence is None:
            self.sequence += 1
            sequence = self.sequence
        return Snapshot(
            run_id=self.run_id,
            snapshot_sequence=sequence,
            world_version=self.world_version,
            tick=self.tick,
            phase=phase,
            self_station_id=station_id,
            rules=self.rules,
            directory=tuple(
                DirectoryEntry(s.station_id, s.display_name or s.station_id)
                for s in self.stations.values()
            ),
            me=self.stations[station_id].observation(),
            offers=tuple(o for o in self.offers if station_id in (o.proposer_id, o.recipient_id)),
            advertisements=tuple(self.advertisements),
            transactions=tuple(
                t for t in self.transactions if station_id in (t.proposer_id, t.recipient_id)
            ),
            request_results=request_results,
            outcome=outcome,
        )

    def outcome_for(self, station_id: str, aborted: bool = False) -> PlayerOutcome:
        """Collective success means every planet reached the end without failing."""
        return PlayerOutcome(
            collective_success=not any(s.failed_once for s in self.stations.values()),
            self_failed=self.stations[station_id].failed_once,
            aborted=aborted,
        )

    # --- commands ---------------------------------------------------------

    def apply(self, station_id: str, actions) -> None:
        """Run a batch of actions in order, keeping the rejections for inspection."""
        for action in actions:
            outcome = self.execute(station_id, action)
            if not outcome.ok:
                self.rejections.append((station_id, outcome.code.name))
        self.world_version += 1

    def execute(self, station_id: str, action) -> CommandOutcome:
        """One command. A command refused before it is processed does not use quota."""
        refused = self._precheck(station_id, action)
        if refused is not None:
            return CommandOutcome(refused)
        self.command_counts[station_id] = self.command_counts.get(station_id, 0) + 1
        if isinstance(action, AdvertiseAction):
            return self._advertise(station_id, action)
        if isinstance(action, OfferAction):
            return self._post_offer(station_id, action)
        if isinstance(action, AcceptAction):
            return self._accept(station_id, action.offer_id)
        if isinstance(action, WithdrawAction):
            return self._withdraw(station_id, action.object_id)
        return CommandOutcome(ResultCode.INVALID_ARGUMENT)

    def _precheck(self, station_id: str, action) -> ResultCode | None:
        station = self.stations[station_id]
        if station.failed_once:
            return ResultCode.STATION_FAILED
        if self.tick >= self.rules.duration_ticks:
            return ResultCode.RUN_NOT_RUNNING
        if self.command_counts.get(station_id, 0) >= self.rules.new_commands_per_station_per_tick:
            return ResultCode.RATE_LIMITED
        if isinstance(action, (OfferAction, AdvertiseAction)):
            ttl = (self.rules.max_offer_ttl_ticks if isinstance(action, OfferAction)
                   else self.rules.max_publication_ttl_ticks)
            if not self.tick < action.expires_tick <= min(self.tick + ttl, self.rules.duration_ticks):
                return ResultCode.INVALID_ARGUMENT
        if isinstance(action, OfferAction):
            recipient = self.stations.get(action.recipient_id)
            if recipient is None or action.recipient_id == station_id or action.give.is_zero():
                return ResultCode.INVALID_ARGUMENT
            if recipient.failed_once:
                return ResultCode.STATION_FAILED
            if not station.inventory.dominates(action.give):
                return ResultCode.INSUFFICIENT_RESOURCES
            open_count = sum(
                o.proposer_id == station_id and o.status is OfferStatus.OPEN for o in self.offers
            )
            if open_count >= self.rules.max_open_outgoing_offers:
                return ResultCode.LIMIT_REACHED
        return None

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    def _advertise(self, station_id: str, action: AdvertiseAction) -> CommandOutcome:
        """Each planet has at most one active advertisement; a new one replaces it."""
        self.advertisements = [a for a in self.advertisements if a.station_id != station_id]
        ad = Advertisement(
            advertisement_id=self._next_id("ad"),
            station_id=station_id,
            selling=action.selling,
            seeking=action.seeking,
            created_tick=self.tick,
            expires_tick=action.expires_tick,
            created_version=self.world_version,
            status=PublicationStatus.ACTIVE,
        )
        self.advertisements.append(ad)
        return CommandOutcome(ResultCode.OK, object_id=ad.advertisement_id)

    def _post_offer(self, station_id: str, action: OfferAction) -> CommandOutcome:
        offer = Offer(
            offer_id=self._next_id("offer"),
            proposer_id=station_id,
            recipient_id=action.recipient_id,
            give=action.give,
            receive=action.receive,
            created_tick=self.tick,
            created_version=self.world_version,
            expires_tick=action.expires_tick,
            status=OfferStatus.OPEN,
            closed_tick=None,
            transaction_id=None,
        )
        self.offers.append(offer)
        return CommandOutcome(ResultCode.OK, object_id=offer.offer_id)

    def _find_offer(self, offer_id: str) -> Offer | None:
        return next((o for o in self.offers if o.offer_id == offer_id), None)

    def _withdraw(self, station_id: str, object_id: str) -> CommandOutcome:
        ad = next((a for a in self.advertisements if a.advertisement_id == object_id), None)
        if ad is not None:
            if ad.station_id != station_id:
                return CommandOutcome(ResultCode.INVALID_ARGUMENT)
            self.advertisements.remove(ad)
            return CommandOutcome(ResultCode.OK, object_id=object_id)
        offer = self._find_offer(object_id)
        if offer is None:
            return CommandOutcome(ResultCode.NOT_FOUND)
        if offer.proposer_id != station_id:
            return CommandOutcome(ResultCode.INVALID_ARGUMENT)
        if offer.status is not OfferStatus.OPEN:
            return CommandOutcome(ResultCode.NOT_OPEN)
        self._close(object_id, OfferStatus.WITHDRAWN)
        return CommandOutcome(ResultCode.OK, object_id=object_id)

    def _close(self, offer_id: str, status: OfferStatus, transaction_id: str | None = None) -> None:
        self.offers = [
            o if o.offer_id != offer_id
            else replace(o, status=status, closed_tick=self.tick, transaction_id=transaction_id)
            for o in self.offers
        ]

    def _accept(self, accepting_id: str, offer_id: str) -> CommandOutcome:
        """Atomic settlement: both bundles move together, or nothing moves."""
        offer = self._find_offer(offer_id)
        if offer is None:
            return CommandOutcome(ResultCode.NOT_FOUND)
        if offer.recipient_id != accepting_id:
            return CommandOutcome(ResultCode.INVALID_ARGUMENT)
        if offer.status is not OfferStatus.OPEN:
            return CommandOutcome(ResultCode.NOT_OPEN)
        if offer.is_expired_at(self.tick):
            return CommandOutcome(ResultCode.EXPIRED)

        proposer = self.stations[offer.proposer_id]
        recipient = self.stations[offer.recipient_id]
        if proposer.failed_once or recipient.failed_once:
            return CommandOutcome(ResultCode.STATION_FAILED)
        if not proposer.inventory.dominates(offer.give) or not recipient.inventory.dominates(offer.receive):
            return CommandOutcome(ResultCode.INSUFFICIENT_RESOURCES)

        proposer.inventory = proposer.inventory - offer.give + offer.receive
        recipient.inventory = recipient.inventory - offer.receive + offer.give
        proposer.exported = proposer.exported + offer.give
        proposer.imported = proposer.imported + offer.receive
        recipient.exported = recipient.exported + offer.receive
        recipient.imported = recipient.imported + offer.give

        transaction_id = self._next_id("txn")
        self._close(offer_id, OfferStatus.ACCEPTED, transaction_id)
        self.transactions.append(
            Transaction(
                transaction_id=transaction_id,
                offer_id=offer.offer_id,
                proposer_id=offer.proposer_id,
                recipient_id=offer.recipient_id,
                give=offer.give,
                receive=offer.receive,
                settled_tick=self.tick,
                settled_version=self.world_version,
            )
        )
        return CommandOutcome(ResultCode.OK, object_id=offer_id, transaction_id=transaction_id)

    # --- time -------------------------------------------------------------

    def advance_tick(self) -> None:
        self.tick += 1
        self.command_counts.clear()
        self.world_version += 1
        for offer in list(self.offers):
            if offer.status is OfferStatus.OPEN and offer.expires_tick <= self.tick:
                self._close(offer.offer_id, OfferStatus.EXPIRED)
        self.advertisements = [a for a in self.advertisements if a.expires_tick > self.tick]
        for station in self.stations.values():
            station.settle_tick(self.rules, self.tick)
        failed = {s.station_id for s in self.stations.values() if s.failed_once}
        self.advertisements = [a for a in self.advertisements if a.station_id not in failed]
        for offer in list(self.offers):
            if offer.status is OfferStatus.OPEN and failed.intersection(
                (offer.proposer_id, offer.recipient_id)
            ):
                self._close(offer.offer_id, OfferStatus.WITHDRAWN)

    @property
    def finished(self) -> bool:
        return self.tick >= self.rules.duration_ticks

    def end_run(self) -> None:
        """Close what is still open once the run's duration is reached."""
        for offer in list(self.offers):
            if offer.status is OfferStatus.OPEN:
                self._close(offer.offer_id, OfferStatus.RUN_ENDED)
        self.advertisements = []
        self.world_version += 1
