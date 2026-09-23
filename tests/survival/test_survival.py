"""Survival behaviour at the boundaries.

Health is the hard constraint: shortage damage compounds and zero health is
permanent, so these cover low stock, changing production, shortage streaks and
failure -- the situations that decide whether the planet lives.
"""

from __future__ import annotations

import pytest

from bazaar_client.domain.types import Bundle, Phase, Resource
from bazaar_client.execution.actions import AdvertiseAction, OfferAction
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.reserves import Urgency, compute_reserve, compute_urgency
from tests.fixtures import factories


def state(**kwargs):
    kwargs.setdefault("self_station_id", "P01")
    return factories.make_snapshot(**kwargs)


def peer_ad(sells, **kwargs):
    return factories.make_advertisement(
        station_id="P02", selling=frozenset({sells}),
        seeking=frozenset({Resource.WATER}), expires_tick=99, **kwargs
    )


# --- shortage recognition -------------------------------------------------


def test_the_specification_shortage_example_is_recognised_as_critical():
    """(2,0,1) plus three water, upkeep one each: food is missing."""
    after_production = Bundle(2, 0, 1) + Bundle(water=3)
    upkeep = Bundle(1, 1, 1)

    urgency = compute_urgency(after_production, upkeep, Bundle(3, 3, 3))

    assert urgency[Resource.FOOD] is Urgency.CRITICAL
    assert urgency[Resource.WATER] is Urgency.NONE


def test_surplus_water_cannot_substitute_for_missing_food():
    """Extra stock of one resource never covers another's upkeep."""
    urgency = compute_urgency(Bundle(100, 0, 100), Bundle(1, 1, 1), Bundle(3, 3, 3))

    assert urgency[Resource.FOOD] is Urgency.CRITICAL


def test_a_starving_station_both_advertises_and_offers_for_what_it_lacks():
    decision, _ = decide(
        state(
            me=factories.make_station(inventory=Bundle(water=40, food=0, components=40)),
            advertisements=(peer_ad(Resource.FOOD),),
        ),
        PolicyMemory(),
    )

    advertised = [a for a in decision.actions if isinstance(a, AdvertiseAction)]
    offered = [a for a in decision.actions if isinstance(a, OfferAction)]

    assert advertised and Resource.FOOD in advertised[0].seeking
    assert offered and offered[0].receive.food > 0


def test_a_starving_station_pays_a_premium_to_settle_faster():
    """Health lost to a shortage cannot be bought back later."""
    calm, _ = decide(
        state(
            me=factories.make_station(inventory=Bundle(water=40, food=2, components=40)),
            advertisements=(peer_ad(Resource.FOOD),),
        ),
        PolicyMemory(),
    )
    desperate, _ = decide(
        state(
            me=factories.make_station(inventory=Bundle(water=40, food=0, components=40)),
            advertisements=(peer_ad(Resource.FOOD),),
        ),
        PolicyMemory(),
    )

    def rate(decision):
        offer = [a for a in decision.actions if isinstance(a, OfferAction) and not a.is_gift][0]
        return offer.give.total() / offer.receive.total()

    assert rate(desperate) > rate(calm)


def test_a_station_short_of_everything_still_keeps_within_budget():
    decision, _ = decide(
        state(
            rules=factories.make_rules(new_commands_per_station_per_tick=3),
            me=factories.make_station(inventory=Bundle.zero()),
            advertisements=(peer_ad(Resource.FOOD),),
        ),
        PolicyMemory(),
    )

    assert len(decision.actions) <= 3


def test_a_station_with_nothing_to_give_does_not_propose_trades():
    """Give something: an offer we cannot pay for would settle nothing."""
    decision, _ = decide(
        state(
            me=factories.make_station(inventory=Bundle.zero()),
            advertisements=(peer_ad(Resource.FOOD),),
        ),
        PolicyMemory(),
    )

    assert [a for a in decision.actions if isinstance(a, OfferAction)] == []


# --- changing production --------------------------------------------------


def test_production_is_read_from_what_actually_arrived():
    """last_production reports the most recent tick, not a forecast."""
    memory = PolicyMemory()
    for tick, produced in enumerate([3, 3, 1], start=1):
        memory.observe(
            state(
                tick=tick,
                me=factories.make_station(last_production=Bundle(water=produced)),
            )
        )

    assert memory.min_recent_production() == 1


def test_production_at_tick_zero_is_not_treated_as_a_measurement():
    """At tick zero last_production is zero because nothing has been produced yet."""
    memory = PolicyMemory()
    memory.observe(state(tick=0, me=factories.make_station(last_production=Bundle.zero())))

    assert memory.min_recent_production() is None


def test_a_falling_specialty_yield_deepens_the_reserve():
    """Output varies over the run, so a dip must widen the buffer."""
    steady = PolicyMemory()
    dipping = PolicyMemory()
    for tick in range(1, 4):
        steady.observe(
            state(tick=tick, me=factories.make_station(last_production=Bundle(water=3)))
        )
        dipping.observe(
            state(tick=tick, me=factories.make_station(last_production=Bundle(water=0)))
        )

    station = factories.make_station(specialty=Resource.WATER)
    rules = factories.make_rules()

    assert (
        compute_reserve(rules, station, dipping).water
        > compute_reserve(rules, station, steady).water
    )


# --- health ---------------------------------------------------------------


def test_a_damaged_station_holds_a_deeper_reserve():
    rules = factories.make_rules()
    memory = PolicyMemory()

    healthy = compute_reserve(rules, factories.make_station(health=100), memory)
    hurt = compute_reserve(rules, factories.make_station(health=20), memory)

    assert hurt.dominates(healthy) and hurt != healthy


def test_a_damaged_station_stops_giving_aid_away():
    """Recovering our own health outranks helping, once we are at risk."""
    memory = PolicyMemory()
    for tick in range(5):
        memory.counterparties.update(
            state(
                tick=tick,
                advertisements=(
                    factories.make_advertisement(
                        station_id="P03", selling=frozenset(),
                        seeking=frozenset({Resource.WATER}),
                        created_tick=tick, expires_tick=tick + 50,
                    ),
                ),
            )
        )

    decision, _ = decide(
        state(
            tick=5,
            me=factories.make_station(health=10, inventory=Bundle(6, 6, 6)),
            advertisements=(
                factories.make_advertisement(
                    station_id="P03", selling=frozenset(),
                    seeking=frozenset({Resource.WATER}),
                    created_tick=5, expires_tick=55,
                ),
            ),
        ),
        memory,
    )

    assert [a for a in decision.actions if isinstance(a, OfferAction) and a.is_gift] == []


# --- permanent failure ----------------------------------------------------


def test_zero_health_ends_trading_permanently():
    """Trading is disabled and gifts can no longer rescue the planet."""
    decision, _ = decide(
        state(me=factories.make_station(health=0, failed_once=True)), PolicyMemory()
    )

    assert decision.actions == []


def test_recovered_health_does_not_undo_a_failure():
    """Passive recovery continues, but it restores neither trading nor the run."""
    decision, _ = decide(
        state(
            me=factories.make_station(
                health=25, failed_once=True, first_failure_tick=12,
                inventory=Bundle(water=40, food=0, components=40),
            ),
            advertisements=(peer_ad(Resource.FOOD),),
        ),
        PolicyMemory(),
    )

    assert decision.actions == []


def test_a_finished_run_stops_new_trading_actions():
    for phase in (Phase.FINISHED, Phase.ABORTED):
        decision, _ = decide(
            state(
                phase=phase,
                me=factories.make_station(inventory=Bundle(water=40, food=0, components=40)),
            ),
            PolicyMemory(),
        )

        assert decision.actions == []


# --- shortage streaks -----------------------------------------------------


@pytest.mark.parametrize("streak", [0, 1, 5])
def test_shortage_counters_are_read_not_recomputed(streak):
    """The server reports these; recomputing them locally would drift."""
    snapshot = state(
        me=factories.make_station(
            current_shortage_streak=streak, shortage_ticks=streak,
            unmet_total=Bundle(food=streak),
        )
    )

    assert snapshot.me.current_shortage_streak == streak
    assert snapshot.me.unmet_total.food == streak
