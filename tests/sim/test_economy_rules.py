"""The economy's rules, each on a hand-checked example.

Upkeep 1 of each resource per tick, 5 health lost per unit short, 5 regained
per fully supplied tick, capped at 100; zero health is permanent.
"""

from __future__ import annotations

from dataclasses import replace

from bazaar_client.domain.types import Bundle, OfferStatus, Resource, ResultCode
from bazaar_client.execution.actions import (
    AcceptAction,
    AdvertiseAction,
    OfferAction,
    WithdrawAction,
)
from bazaar_sim.economy import DEFAULT_RULES, SimStation, SimulatedEconomy


def economy(p01=Bundle(10, 10, 10), p02=Bundle(10, 10, 10), production=2, **rules):
    return SimulatedEconomy(
        SimStation("P01", Resource.WATER, p01, production=production),
        SimStation("P02", Resource.FOOD, p02, production=production),
        rules=replace(DEFAULT_RULES, **rules),
    )


def test_a_tick_produces_the_specialty_then_consumes_upkeep():
    world = economy()
    world.advance_tick()

    p01 = world.stations["P01"]
    assert p01.inventory == Bundle(10 + 2 - 1, 10 - 1, 10 - 1)
    assert (p01.produced_total, p01.consumed_total) == (Bundle(water=2), Bundle(1, 1, 1))
    assert p01.health == 100 and p01.fully_supplied_ticks == 1


def test_each_unit_short_costs_five_health_and_a_full_tick_restores_five():
    world = economy(p01=Bundle(10, 0, 0), production=0)
    world.advance_tick()
    p01 = world.stations["P01"]
    assert p01.health == 100 - 2 * 5  # food and components each one short
    assert p01.last_unmet == Bundle(0, 1, 1) and p01.current_shortage_streak == 1

    p01.inventory = Bundle(5, 5, 5)
    world.advance_tick()
    assert p01.health == 95 and p01.current_shortage_streak == 0
    world.advance_tick()
    world.advance_tick()
    assert p01.health == 100  # capped at max_health
    assert p01.longest_shortage_streak == 1


def test_zero_health_is_permanent_and_closes_the_planets_dealings():
    world = economy(p01=Bundle(0, 0, 0), production=0)
    world.execute("P02", OfferAction("P01", Bundle(food=1), Bundle.zero(), 10))  # still open at 7
    for _ in range(7):  # 3 units short a tick: 15 health a tick, 100 -> 0 in 7 ticks
        world.advance_tick()

    p01 = world.stations["P01"]
    assert (p01.health, p01.failed_once, p01.first_failure_tick) == (0, True, 7)
    assert world.offers[0].status is OfferStatus.WITHDRAWN
    p01.inventory = Bundle(50, 50, 50)
    world.advance_tick()
    assert p01.failed_once and world.execute("P01", _ad()).code is ResultCode.STATION_FAILED


def _ad(expires=5):
    return AdvertiseAction(frozenset({Resource.WATER}), frozenset({Resource.FOOD}), expires)


def test_one_advertisement_per_planet_the_newest_replaces_the_last():
    world = economy()
    first = world.execute("P01", _ad())
    second = world.execute("P01", _ad(expires=6))

    assert first.ok and second.ok
    assert [a.advertisement_id for a in world.advertisements] == [second.object_id]


def test_offer_arguments_are_validated_before_anything_changes():
    world = economy()
    too_long = OfferAction("P02", Bundle(water=1), Bundle(food=1), DEFAULT_RULES.max_offer_ttl_ticks + 1)
    to_self = OfferAction("P01", Bundle(water=1), Bundle(food=1), 5)
    empty = OfferAction("P02", Bundle.zero(), Bundle(food=1), 5)
    unaffordable = OfferAction("P02", Bundle(water=11), Bundle(food=1), 5)

    codes = [world.execute("P01", o).code for o in (too_long, to_self, empty, unaffordable)]

    assert codes == [ResultCode.INVALID_ARGUMENT] * 3 + [ResultCode.INSUFFICIENT_RESOURCES]
    assert world.offers == [] and world.command_counts.get("P01", 0) == 0


def test_open_offers_are_capped_per_planet():
    world = economy(max_open_outgoing_offers=2, new_commands_per_station_per_tick=10)
    codes = [world.execute("P01", OfferAction("P02", Bundle(water=1), Bundle(food=1), 5)).code
             for _ in range(3)]

    assert codes == [ResultCode.OK, ResultCode.OK, ResultCode.LIMIT_REACHED]


def test_only_the_recipient_accepts_and_only_the_proposer_withdraws():
    world = economy()
    offer_id = world.execute("P01", OfferAction("P02", Bundle(water=1), Bundle(food=1), 5)).object_id

    assert world.execute("P01", AcceptAction(offer_id)).code is ResultCode.INVALID_ARGUMENT
    assert world.execute("P02", WithdrawAction(offer_id)).code is ResultCode.INVALID_ARGUMENT
    assert world.execute("P02", AcceptAction("offer-404")).code is ResultCode.NOT_FOUND
    assert world.execute("P01", WithdrawAction(offer_id)).ok
    assert world.execute("P01", WithdrawAction(offer_id)).code is ResultCode.NOT_OPEN
    assert world.offers[0].closed_tick == 0


def test_a_settled_trade_records_both_sides_and_its_transaction():
    world = economy()
    offer_id = world.execute("P01", OfferAction("P02", Bundle(water=3), Bundle(food=2), 5)).object_id
    taken = world.execute("P02", AcceptAction(offer_id))

    assert taken.ok and taken.transaction_id == world.transactions[0].transaction_id
    assert world.stations["P01"].inventory == Bundle(7, 12, 10)
    assert world.stations["P02"].inventory == Bundle(13, 8, 10)
    assert world.stations["P01"].exported == Bundle(water=3)
    assert world.stations["P02"].imported == Bundle(water=3)
    assert world.offers[0].status is OfferStatus.ACCEPTED


def test_the_end_of_the_run_closes_everything_still_open():
    world = economy(duration_ticks=2)
    world.execute("P01", OfferAction("P02", Bundle(water=1), Bundle(food=1), 2))
    world.execute("P01", _ad(expires=2))
    world.advance_tick()
    world.advance_tick()

    assert world.finished
    assert world.execute("P01", _ad()).code is ResultCode.RUN_NOT_RUNNING
    world.end_run()
    assert world.advertisements == []
    assert world.outcome_for("P01").collective_success is True
