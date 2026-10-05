"""Stock above the cap goes to planets that advertise a need for it.

Trading is untouched: a beneficial trade is still taken even when it takes us
over the cap. The cap is enforced afterwards, by giving away, never by refusing.
"""

from __future__ import annotations

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.execution.actions import AcceptAction, OfferAction
from bazaar_client.policy.altruism import MAX_EXCESS_GIFT
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.reserves import STOCK_CAP, excess_over_cap
from bazaar_sim.world import balanced_production
from tests.fixtures import factories


def seeker(station_id, resource, tick=0):
    return factories.make_advertisement(
        station_id=station_id,
        selling=frozenset(),
        seeking=frozenset({resource}),
        created_tick=tick,
        expires_tick=tick + 50,
    )


def state(inventory, *, ads=(), offers=(), tick=0, budget=1):
    return factories.make_snapshot(
        self_station_id="P01",
        tick=tick,
        me=factories.make_station(inventory=inventory),
        advertisements=tuple(ads),
        offers=tuple(offers),
        rules=factories.make_rules(new_commands_per_station_per_tick=budget),
    )


def gifts(decision):
    return [a for a in decision.actions
            if isinstance(a, OfferAction) and a.receive.is_zero()]


def test_excess_is_measured_above_the_cap():
    assert excess_over_cap(Bundle(STOCK_CAP + 5, STOCK_CAP, 3)) == Bundle(5, 0, 0)


def test_stock_over_the_cap_is_gifted_to_a_seeker_before_new_offers():
    decision, _ = decide(
        state(Bundle(40, 40, 130), ads=[seeker("P04", Resource.COMPONENTS)]),
        PolicyMemory(),
    )

    first = decision.actions[0]
    assert isinstance(first, OfferAction)
    assert first.recipient_id == "P04"
    assert first.give == Bundle(components=MAX_EXCESS_GIFT)
    assert first.receive.is_zero()


def test_a_small_excess_is_gifted_exactly():
    decision, _ = decide(
        state(Bundle(40, 40, STOCK_CAP + 5), ads=[seeker("P04", Resource.COMPONENTS)]),
        PolicyMemory(),
    )

    assert [g.give for g in gifts(decision)] == [Bundle(components=5)]


def test_nothing_is_gifted_when_nobody_is_asking():
    decision, _ = decide(
        state(Bundle(40, 40, 130), ads=[seeker("P04", Resource.FOOD)]),
        PolicyMemory(),
    )

    assert not [g for g in gifts(decision) if g.give.components]


def test_a_beneficial_accept_still_wins_the_command():
    offer = factories.make_offer(
        offer_id="good", proposer_id="P03", recipient_id="P01",
        give=Bundle(food=4), receive=Bundle(water=4), expires_tick=10,
    )
    decision, _ = decide(
        state(Bundle(60, 15, 130), ads=[seeker("P04", Resource.COMPONENTS)],
              offers=[offer]),
        PolicyMemory(),
    )

    assert decision.actions == [AcceptAction("good")]


def test_we_never_offer_to_buy_more_of_what_we_are_giving_away():
    # Components and food both high, so the balance ceiling alone would allow more.
    decision, _ = decide(
        state(Bundle(60, 125, 125), budget=4, ads=[
            seeker("P04", Resource.FOOD),
            factories.make_advertisement(
                station_id="P05", selling=frozenset({Resource.FOOD, Resource.COMPONENTS}),
                seeking=frozenset({Resource.WATER}), created_tick=0, expires_tick=50),
        ]),
        PolicyMemory(),
    )

    buys = [a for a in decision.actions if isinstance(a, OfferAction) and not a.receive.is_zero()]
    assert all(a.receive.food == 0 and a.receive.components == 0 for a in buys)


def test_wants_stop_at_the_cap():
    decision, _ = decide(
        state(Bundle(60, 97, 98), budget=4, ads=[
            factories.make_advertisement(
                station_id="P05", selling=frozenset({Resource.FOOD, Resource.COMPONENTS}),
                seeking=frozenset({Resource.WATER}), created_tick=0, expires_tick=50),
        ]),
        PolicyMemory(),
    )

    for offer in [a for a in decision.actions if isinstance(a, OfferAction)]:
        assert offer.receive.food <= 3 and offer.receive.components <= 2


def test_gifts_rotate_between_seekers():
    ads = [seeker("P04", Resource.COMPONENTS), seeker("P05", Resource.COMPONENTS)]
    memory = PolicyMemory()
    recipients = []
    for tick in (0, 1):
        decision, memory = decide(state(Bundle(40, 40, 160), ads=ads, tick=tick), memory)
        recipients += [g.recipient_id for g in gifts(decision)]

    assert sorted(recipients) == ["P04", "P05"]


def test_surplus_world_produces_the_requested_extra():
    exact = balanced_production(9)
    rich = balanced_production(9, surplus=0.25)
    ticks = range(120)
    base = sum(exact(t, i) for t in ticks for i in range(9))
    more = sum(rich(t, i) for t in ticks for i in range(9))

    assert abs(more / base - 1.25) < 0.01
