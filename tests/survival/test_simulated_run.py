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

import pytest

from bazaar_client.domain.types import Bundle, OfferStatus, Resource
from bazaar_client.execution.actions import AcceptAction, OfferAction
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.world.commitments import CommitmentTracker
from bazaar_sim.economy import DEFAULT_RULES, SimStation, SimulatedEconomy

RULES = DEFAULT_RULES
US = "P01"


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


@pytest.mark.parametrize("production", [2, 3, 5])
def test_the_planet_survives_across_production_levels(production):
    """Surplus describes the whole run, not every moment of it."""
    economy = build_economy(production=production)

    run_simulation(economy, ticks=30)

    assert economy.stations[US].health > 0


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


@pytest.mark.parametrize("station_count", [3, 9])
def test_actual_thirty_unit_start_survives_a_full_run(station_count):
    economy = SimulatedEconomy(*(
        SimStation(f"P{i + 1:02}", list(Resource)[i % 3], Bundle(30, 30, 30), production=4)
        for i in range(station_count)
    ))
    run_simulation(economy, ticks=120)
    assert all(not s.failed_once and s.health == 100 for s in economy.stations.values())
    assert all(s.imported.total() > 0 for s in economy.stations.values())
    assert not economy.rejections
