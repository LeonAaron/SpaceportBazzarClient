"""A multi-tick simulated economy.

The practice server runs one fixed script, so it cannot exercise the trading
policy over time: anything but the scripted commands ends the exercise as a
scenario mismatch. This harness fills that gap by modelling the economy the
field manual describes -- production, upkeep, shortage damage, recovery -- and
running the real `decide` against it for many ticks.

It is a simulation, not the real server: counterparty behaviour here is a
stand-in for nine student clients, and settlement is modelled rather than
observed. What it does prove is that the policy keeps its own planet supplied
under scarcity and does not trade itself to death.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import pytest

from bazaar_client.domain.types import (
    Advertisement,
    Bundle,
    DirectoryEntry,
    Offer,
    OfferStatus,
    Phase,
    PublicationStatus,
    Resource,
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
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.world.commitments import CommitmentTracker
from tests.fixtures import factories

RULES = factories.make_rules(
    max_health=100,
    shortage_damage_per_unit=5,
    recovery_per_fully_supplied_tick=5,
    new_commands_per_station_per_tick=4,
    max_open_outgoing_offers=6,
)
UPKEEP = Bundle(1, 1, 1)
US = "P01"


@dataclass
class SimStation:
    station_id: str
    specialty: Resource
    inventory: Bundle
    production: int = 3
    health: int = 100
    failed_once: bool = False
    shortage_ticks: int = 0
    fully_supplied_ticks: int = 0
    last_production: Bundle = field(default_factory=Bundle.zero)
    last_unmet: Bundle = field(default_factory=Bundle.zero)
    imported: Bundle = field(default_factory=Bundle.zero)
    exported: Bundle = field(default_factory=Bundle.zero)

    def settle_tick(self) -> None:
        produced = Bundle.single(self.specialty, self.production)
        self.inventory = self.inventory + produced
        self.last_production = produced

        unmet = UPKEEP.saturating_sub(self.inventory)
        self.inventory = self.inventory.saturating_sub(UPKEEP)
        self.last_unmet = unmet

        if unmet.is_zero():
            self.health = min(RULES.max_health, self.health + RULES.recovery_per_fully_supplied_tick)
            self.fully_supplied_ticks += 1
        else:
            self.health = max(0, self.health - unmet.total() * RULES.shortage_damage_per_unit)
            self.shortage_ticks += 1
            if self.health == 0:
                self.failed_once = True


class SimulatedEconomy:
    """A configurable roster trading under the published limits."""

    def __init__(self, *stations: SimStation, peers_accept: bool = True) -> None:
        self.stations = {s.station_id: s for s in stations}
        self.tick = 0
        self.world_version = 1
        self.sequence = 0
        self.offers: list[Offer] = []
        self.transactions: list[Transaction] = []
        self.advertisements: list[Advertisement] = []
        self.peers_accept = peers_accept
        self._ids = itertools.count(1)
        self.command_counts: dict[str, int] = {}
        self.rejections: list[tuple[str, str]] = []

    # --- snapshot construction -------------------------------------------

    def observation_for(self, station_id: str) -> Snapshot:
        station = self.stations[station_id]
        self.sequence += 1
        return Snapshot(
            run_id="sim",
            snapshot_sequence=self.sequence,
            world_version=self.world_version,
            tick=self.tick,
            phase=Phase.RUNNING,
            self_station_id=station_id,
            rules=RULES,
            directory=tuple(
                DirectoryEntry(s.station_id, s.station_id) for s in self.stations.values()
            ),
            me=StationObservation(
                station_id=station_id,
                inventory=station.inventory,
                health=station.health,
                failed_once=station.failed_once,
                first_failure_tick=None,
                last_production=station.last_production,
                last_unmet_upkeep=station.last_unmet,
                fully_supplied_ticks=station.fully_supplied_ticks,
                shortage_ticks=station.shortage_ticks,
                current_shortage_streak=0,
                longest_shortage_streak=0,
                produced_total=Bundle.zero(),
                consumed_total=Bundle.zero(),
                unmet_total=Bundle.zero(),
                imported_total=station.imported,
                exported_total=station.exported,
                upkeep_per_tick=UPKEEP,
                specialty=station.specialty,
            ),
            offers=tuple(o for o in self.offers if station_id in (o.proposer_id, o.recipient_id)),
            advertisements=tuple(self.advertisements),
            transactions=tuple(t for t in self.transactions if station_id in (t.proposer_id, t.recipient_id)),
            request_results=(),
            outcome=None,
        )

    # --- applying our client's actions ------------------------------------

    def apply(self, station_id: str, actions) -> None:
        for action in actions:
            reason = self.rejection_reason(station_id, action)
            if reason:
                self.rejections.append((station_id, reason))
                continue
            self.command_counts[station_id] = self.command_counts.get(station_id, 0) + 1
            if isinstance(action, AdvertiseAction):
                self._advertise(station_id, action)
            elif isinstance(action, OfferAction):
                self._post_offer(station_id, action)
            elif isinstance(action, AcceptAction):
                if not self._accept(station_id, action.offer_id):
                    self.rejections.append((station_id, "ACCEPT_REJECTED"))
            elif isinstance(action, WithdrawAction):
                self._withdraw(action.object_id)
        self.world_version += 1

    def rejection_reason(self, station_id, action):
        station = self.stations[station_id]
        if station.failed_once:
            return "STATION_FAILED"
        if self.tick >= RULES.duration_ticks:
            return "RUN_NOT_RUNNING"
        if self.command_counts.get(station_id, 0) >= RULES.new_commands_per_station_per_tick:
            return "RATE_LIMITED"
        if isinstance(action, (OfferAction, AdvertiseAction)):
            ttl = (RULES.max_offer_ttl_ticks if isinstance(action, OfferAction)
                   else RULES.max_publication_ttl_ticks)
            if not self.tick < action.expires_tick <= min(self.tick + ttl, RULES.duration_ticks):
                return "INVALID_ARGUMENT"
        if isinstance(action, OfferAction):
            if self.stations[action.recipient_id].failed_once:
                return "STATION_FAILED"
            if not station.inventory.dominates(action.give):
                return "INSUFFICIENT_RESOURCES"
            count = sum(o.proposer_id == station_id and o.status is OfferStatus.OPEN
                        for o in self.offers)
            if count >= RULES.max_open_outgoing_offers:
                return "LIMIT_REACHED"
        return None

    def _advertise(self, station_id: str, action: AdvertiseAction) -> None:
        self.advertisements = [a for a in self.advertisements if a.station_id != station_id]
        self.advertisements.append(
            Advertisement(
                advertisement_id=f"ad-{next(self._ids)}",
                station_id=station_id,
                selling=action.selling,
                seeking=action.seeking,
                created_tick=self.tick,
                expires_tick=action.expires_tick,
                created_version=self.world_version,
                status=PublicationStatus.ACTIVE,
            )
        )

    def _post_offer(self, station_id: str, action: OfferAction) -> None:
        self.offers.append(
            Offer(
                offer_id=f"offer-{next(self._ids)}",
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
        )

    def _withdraw(self, object_id: str) -> None:
        self.advertisements = [
            a for a in self.advertisements if a.advertisement_id != object_id
        ]
        self._close(object_id, OfferStatus.WITHDRAWN)

    def _close(self, offer_id: str, status: OfferStatus) -> None:
        self.offers = [
            (o if o.offer_id != offer_id else self._with_status(o, status))
            for o in self.offers
        ]

    @staticmethod
    def _with_status(offer: Offer, status: OfferStatus, transaction_id=None) -> Offer:
        return Offer(
            offer_id=offer.offer_id,
            proposer_id=offer.proposer_id,
            recipient_id=offer.recipient_id,
            give=offer.give,
            receive=offer.receive,
            created_tick=offer.created_tick,
            created_version=offer.created_version,
            expires_tick=offer.expires_tick,
            status=status,
            closed_tick=offer.created_tick,
            transaction_id=transaction_id,
        )

    def _accept(self, accepting_id: str, offer_id: str) -> bool:
        """Atomic settlement: both bundles move together, or nothing moves."""
        offer = next((o for o in self.offers if o.offer_id == offer_id), None)
        if offer is None or offer.status is not OfferStatus.OPEN:
            return False
        if offer.is_expired_at(self.tick):
            return False
        if offer.recipient_id != accepting_id:
            return False

        proposer = self.stations[offer.proposer_id]
        recipient = self.stations[offer.recipient_id]
        if proposer.failed_once or recipient.failed_once:
            return False
        if not proposer.inventory.dominates(offer.give):
            return False
        if not recipient.inventory.dominates(offer.receive):
            return False

        proposer.inventory = proposer.inventory - offer.give + offer.receive
        recipient.inventory = recipient.inventory - offer.receive + offer.give
        proposer.exported = proposer.exported + offer.give
        proposer.imported = proposer.imported + offer.receive
        recipient.exported = recipient.exported + offer.receive
        recipient.imported = recipient.imported + offer.give

        transaction_id = f"txn-{next(self._ids)}"
        self.offers = [
            (o if o.offer_id != offer_id else self._with_status(o, OfferStatus.ACCEPTED, transaction_id))
            for o in self.offers
        ]
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
        return True

    # --- time -------------------------------------------------------------

    def advance_tick(self) -> None:
        self.tick += 1
        self.command_counts.clear()
        self.world_version += 1
        self.offers = [
            (o if not (o.status is OfferStatus.OPEN and o.expires_tick <= self.tick)
             else self._with_status(o, OfferStatus.EXPIRED))
            for o in self.offers
        ]
        self.advertisements = [a for a in self.advertisements if a.expires_tick > self.tick]
        for station in self.stations.values():
            station.settle_tick()
        failed = {s.station_id for s in self.stations.values() if s.failed_once}
        self.advertisements = [a for a in self.advertisements if a.station_id not in failed]
        for offer in list(self.offers):
            if offer.status is OfferStatus.OPEN and failed.intersection(
                (offer.proposer_id, offer.recipient_id)
            ):
                self._close(offer.offer_id, OfferStatus.WITHDRAWN)


def build_economy(**overrides) -> SimulatedEconomy:
    return SimulatedEconomy(
        SimStation(US, Resource.WATER, Bundle(5, 5, 5), **overrides),
        SimStation("P02", Resource.FOOD, Bundle(5, 5, 5)),
        SimStation("P03", Resource.COMPONENTS, Bundle(5, 5, 5)),
    )


class SimulatedClients:
    """Every planet runs this same policy, as every student pair will.

    Stations act in turn within a tick, so an offer posted by one is visible to
    the next -- the same way a counterparty sees it in their next snapshot.
    """

    def __init__(self, economy: SimulatedEconomy, trading: set[str] | None = None) -> None:
        self.economy = economy
        self.trading = trading if trading is not None else set(economy.stations)
        self.memories = {sid: PolicyMemory() for sid in economy.stations}
        self.commitments = {sid: CommitmentTracker() for sid in economy.stations}

    def step(self) -> None:
        for station_id in self.economy.stations:
            if station_id not in self.trading:
                continue
            if self.economy.stations[station_id].failed_once:
                continue
            observation = self.economy.observation_for(station_id)
            decision, self.memories[station_id] = decide(
                observation, self.memories[station_id], self.commitments[station_id]
            )
            self.economy.apply(station_id, decision.actions)

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.step()
            self.economy.advance_tick()


def run_simulation(economy: SimulatedEconomy, ticks: int, **kwargs) -> SimulatedClients:
    clients = SimulatedClients(economy, **kwargs)
    clients.run(ticks)
    return clients


# --- the tests ------------------------------------------------------------


def test_the_planet_survives_a_long_run():
    """The first duty: keep our own planet supplied."""
    economy = build_economy()

    run_simulation(economy, ticks=40)

    us = economy.stations[US]
    assert us.health > 0
    assert not us.failed_once


def test_trading_actually_brings_in_what_we_cannot_produce():
    """One specialty, three needs: survival depends on exchange, not production."""
    economy = build_economy()

    run_simulation(economy, ticks=30)

    us = economy.stations[US]
    assert us.imported.food > 0
    assert us.imported.components > 0
    assert us.exported.water > 0


def test_health_stays_high_when_the_market_functions():
    economy = build_economy()

    run_simulation(economy, ticks=40)

    assert economy.stations[US].health >= 50


def test_no_counterparty_is_traded_into_the_ground():
    """Our shared duty is that all planets survive, not just ours."""
    economy = build_economy()

    run_simulation(economy, ticks=40)

    assert all(s.health > 0 for s in economy.stations.values())


def test_the_planet_holds_out_longer_than_doing_nothing():
    """A passive station starves; the policy has to beat that baseline."""
    traded = build_economy()
    run_simulation(traded, ticks=25)

    passive = build_economy()
    for _ in range(25):
        passive.advance_tick()

    assert traded.stations[US].health > passive.stations[US].health


def test_a_market_that_never_accepts_does_not_cause_reckless_giveaway():
    """If nobody else trades we cannot be saved, but we must not hasten our end."""
    economy = build_economy()

    run_simulation(economy, ticks=20, trading={US})

    us = economy.stations[US]
    # Water is ours to produce; an unanswered market must not drain it.
    assert us.inventory.water > 0
    assert us.exported.is_zero()


def test_open_offers_never_promise_more_than_we_hold():
    """Posting reserves nothing, so several offers must not oversubscribe stock."""
    economy = build_economy()
    clients = SimulatedClients(economy)

    for _ in range(25):
        clients.step()

        promised = Bundle.zero()
        for offer in economy.offers:
            if offer.proposer_id == US and offer.status is OfferStatus.OPEN:
                promised = promised + offer.give
        assert economy.stations[US].inventory.dominates(promised), (
            f"promised {promised.as_dict()} holding "
            f"{economy.stations[US].inventory.as_dict()}"
        )

        economy.advance_tick()


@pytest.mark.parametrize("production", [3, 5])
def test_the_planet_survives_across_production_levels(production):
    """Surplus describes the whole run, not every moment of it."""
    economy = build_economy(production=production)

    run_simulation(economy, ticks=30)

    assert economy.stations[US].health > 0


def test_a_starved_economy_is_outlasted_without_ever_trading_below_parity():
    """At production 2 with 5 units of starting stock, the whole economy makes
    less water than it consumes, so strict 1:1 cannot import two units a tick
    for the one we can spare. We refuse to overpay, but must still beat doing
    nothing. The real run avoids this: low phases last 12 ticks and are paid
    for from specialty stock built up in the high ones.
    """
    traded = build_economy(production=2)
    run_simulation(traded, ticks=30)

    passive = build_economy(production=2)
    for _ in range(30):
        passive.advance_tick()

    assert traded.stations[US].shortage_ticks < passive.stations[US].shortage_ticks
    assert all_trades_at_parity(traded)


@pytest.mark.parametrize("disrupted", [False, True])
def test_nine_planets_with_different_response_times_and_variable_production(disrupted):
    economy = SimulatedEconomy(*(
        SimStation(f"P{i + 1:02}", list(Resource)[i % 3], Bundle(12, 12, 12), production=4)
        for i in range(9)
    ))
    clients = SimulatedClients(economy)
    for tick in range(60):
        for i, station in enumerate(economy.stations.values()):
            # Production is a schedule, not a promised constant. Two peers have
            # multi-tick outages; patient peers only accept every second tick.
            station.production = 2 if disrupted and (tick + i) % 11 < 3 else 4
            if disrupted and i in (2, 5) and 12 <= tick < 16:
                continue
            observation = economy.observation_for(station.station_id)
            decision, clients.memories[station.station_id] = decide(
                observation, clients.memories[station.station_id],
                clients.commitments[station.station_id],
            )
            actions = decision.actions
            if disrupted and i % 3 == 1 and tick % 2:
                actions = [a for a in actions if not isinstance(a, AcceptAction)]
            economy.apply(station.station_id, actions)
            promised = sum_bundles(
                o.give for o in economy.offers
                if o.proposer_id == station.station_id and o.status is OfferStatus.OPEN
            )
            assert station.inventory.dominates(promised)
        economy.advance_tick()
    assert all(not s.failed_once for s in economy.stations.values())
    assert all(s.imported.total() > 0 for s in economy.stations.values())
    assert not economy.rejections


def all_trades_at_parity(economy) -> bool:
    """Every settlement was a gift or exactly one-for-one."""
    return all(
        t.receive.is_zero() or t.give.total() == t.receive.total()
        for t in economy.transactions
    )


PHASES = (2, 5, 6)
PHASE_TICKS = 12


def test_run_two_replayed_every_trading_planet_survives():
    """The class run that failed: nine planets, 30 of everything at the start,
    production cycling 2/5/6 in 12-tick phases, one planet whose client never
    connected (P02) and one that went quiet at tick 45 (P05).

    Every planet still trading must reach the end of the 120-tick run.
    """
    economy = SimulatedEconomy(*(
        SimStation(f"P{i + 1:02}", list(Resource)[i % 3], Bundle(30, 30, 30))
        for i in range(9)
    ))
    silent, quits = "P02", "P05"
    clients = SimulatedClients(economy, trading=set(economy.stations) - {silent})

    for tick in range(120):
        for i, station in enumerate(economy.stations.values()):
            station.production = PHASES[(tick // PHASE_TICKS + i) % len(PHASES)]
        if tick == 45:
            clients.trading.discard(quits)
        clients.step()
        economy.advance_tick()

    survivors = {sid for sid, s in economy.stations.items() if not s.failed_once}
    assert survivors >= set(economy.stations) - {silent, quits}
    assert all_trades_at_parity(economy)
    # Commands addressed to the planet that quit and then failed are refused;
    # the live executor marks such a station failed on the first refusal.
    assert {code for _, code in economy.rejections} <= {"STATION_FAILED"}


def test_run_two_replayed_we_never_pay_with_what_we_cannot_produce():
    """Run 2: P01 paid and gifted water it could not make, then starved of it."""
    economy = SimulatedEconomy(*(
        SimStation(f"P{i + 1:02}", list(Resource)[i % 3], Bundle(30, 30, 30))
        for i in range(9)
    ))
    clients = SimulatedClients(economy)
    for tick in range(60):
        for i, station in enumerate(economy.stations.values()):
            station.production = PHASES[(tick // PHASE_TICKS + i) % len(PHASES)]
        clients.step()
        economy.advance_tick()

    specialty = {sid: s.specialty for sid, s in economy.stations.items()}
    for txn in economy.transactions:
        paid_by_proposer = txn.give
        paid_by_recipient = txn.receive
        assert set(r for r in Resource if paid_by_proposer.get(r)) <= {specialty[txn.proposer_id]}
        assert set(r for r in Resource if paid_by_recipient.get(r)) <= {specialty[txn.recipient_id]}


def sum_bundles(bundles):
    total = Bundle.zero()
    for bundle in bundles:
        total += bundle
    return total


def test_simulator_rejects_expired_and_unaffordable_acceptances_atomically():
    economy = build_economy()
    economy.apply(US, [OfferAction("P02", Bundle(water=4), Bundle(food=4), 2)])
    offer_id = economy.offers[-1].offer_id
    economy.stations["P02"].inventory = Bundle.zero()
    before = {sid: s.inventory for sid, s in economy.stations.items()}
    economy.apply("P02", [AcceptAction(offer_id)])
    assert {sid: s.inventory for sid, s in economy.stations.items()} == before
    assert economy.offers[-1].status is OfferStatus.OPEN
    economy.advance_tick()
    economy.advance_tick()
    before = {sid: s.inventory for sid, s in economy.stations.items()}
    economy.apply("P02", [AcceptAction(offer_id)])
    assert {sid: s.inventory for sid, s in economy.stations.items()} == before
    assert len(economy.rejections) == 2
