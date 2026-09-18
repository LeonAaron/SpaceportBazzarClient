"""Reserves, urgency and pricing."""

from __future__ import annotations

import pytest

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.pricing import (
    MAX_PREMIUM_RATIO,
    MAX_TRADE_SIZE,
    CannotAfford,
    compute_terms,
    desired_quantity,
    premium_for,
)
from bazaar_client.policy.reserves import (
    MAX_SAFETY_TICKS,
    MIN_SAFETY_TICKS,
    Urgency,
    compute_reserve,
    compute_urgency,
    deficit_below_reserve,
    safety_ticks,
    surplus_above_reserve,
)
from tests.fixtures import factories


def memory_with(round_trips=(), productions=()):
    memory = PolicyMemory()
    memory.round_trips.extend(round_trips)
    memory.productions.extend(productions)
    return memory


# --- reserves -------------------------------------------------------------


def test_reserve_scales_with_the_published_upkeep():
    """Upkeep comes from the snapshot, so a different rule set changes the reserve."""
    station = factories.make_station(upkeep_per_tick=Bundle(2, 1, 3))
    reserve = compute_reserve(factories.make_rules(), station, PolicyMemory())

    assert reserve == Bundle(2, 1, 3).scale(MIN_SAFETY_TICKS)


def test_reserve_uses_the_default_buffer_before_any_trade_is_observed():
    assert safety_ticks(
        Resource.WATER, factories.make_rules(), factories.make_station(), PolicyMemory()
    ) == MIN_SAFETY_TICKS


def test_reserve_grows_when_trades_are_observed_to_be_slow():
    """A slow market needs a deeper buffer; the figure is measured, not guessed."""
    memory = memory_with(round_trips=(5, 6, 5))

    # median 5, plus one tick of slack
    assert safety_ticks(
        Resource.WATER, factories.make_rules(), factories.make_station(), memory
    ) == 6


def test_a_single_sample_is_not_enough_to_move_the_buffer():
    memory = memory_with(round_trips=(9,))

    assert safety_ticks(
        Resource.WATER, factories.make_rules(), factories.make_station(), memory
    ) == MIN_SAFETY_TICKS


def test_low_health_deepens_the_buffer():
    """Shortage damage compounds, so recovery deserves extra margin."""
    station = factories.make_station(health=40)

    assert safety_ticks(
        Resource.WATER, factories.make_rules(), station, PolicyMemory()
    ) == MIN_SAFETY_TICKS + 2


def test_a_production_dip_deepens_the_buffer_for_our_own_specialty():
    """Production varies over the run, so a dip must not be assumed temporary."""
    station = factories.make_station(specialty=Resource.WATER, upkeep_per_tick=Bundle(1, 1, 1))
    memory = memory_with(productions=(3, 0, 2))

    assert safety_ticks(Resource.WATER, factories.make_rules(), station, memory) == MIN_SAFETY_TICKS + 1
    assert safety_ticks(Resource.FOOD, factories.make_rules(), station, memory) == MIN_SAFETY_TICKS


def test_the_buffer_is_capped_so_we_cannot_hoard_forever():
    memory = memory_with(round_trips=(40, 40, 40))
    station = factories.make_station(health=1)

    assert safety_ticks(
        Resource.WATER, factories.make_rules(), station, memory
    ) == MAX_SAFETY_TICKS


# --- urgency --------------------------------------------------------------


def test_urgency_is_critical_when_the_next_upkeep_is_not_covered():
    urgency = compute_urgency(Bundle(0, 5, 5), Bundle(1, 1, 1), Bundle(3, 3, 3))

    assert urgency[Resource.WATER] is Urgency.CRITICAL


def test_urgency_is_watch_between_upkeep_and_reserve():
    urgency = compute_urgency(Bundle(2, 5, 5), Bundle(1, 1, 1), Bundle(3, 3, 3))

    assert urgency[Resource.WATER] is Urgency.WATCH


def test_urgency_is_none_at_or_above_reserve():
    urgency = compute_urgency(Bundle(3, 3, 3), Bundle(1, 1, 1), Bundle(3, 3, 3))

    assert all(level is Urgency.NONE for level in urgency.values())


def test_surplus_and_deficit_are_measured_against_the_reserve():
    assert surplus_above_reserve(Bundle(10, 1, 3), Bundle(3, 3, 3)) == Bundle(7, 0, 0)
    assert deficit_below_reserve(Bundle(10, 1, 3), Bundle(3, 3, 3)) == Bundle(0, 2, 0)


# --- pricing --------------------------------------------------------------


def test_the_baseline_price_is_one_for_one():
    """Every planet consumes all three resources, so parity is the neutral price."""
    give, receive = compute_terms(
        Resource.FOOD, 2, Resource.WATER, Bundle(water=10), Urgency.NONE
    )

    assert give == Bundle(water=2)
    assert receive == Bundle(food=2)


def test_urgency_sweetens_our_side_to_buy_speed():
    give, receive = compute_terms(
        Resource.FOOD, 2, Resource.WATER, Bundle(water=10), Urgency.CRITICAL
    )

    assert receive == Bundle(food=2)
    assert give.water == 3  # 2 * 1.5


@pytest.mark.parametrize("urgency", list(Urgency))
def test_the_premium_never_exceeds_the_cap(urgency):
    assert premium_for(urgency) <= MAX_PREMIUM_RATIO


@pytest.mark.parametrize("want_qty", range(1, 9))
@pytest.mark.parametrize("urgency", list(Urgency))
def test_the_effective_rate_never_exceeds_the_cap(want_qty, urgency):
    """Rounding must not push the real price past the declared ceiling."""
    give, receive = compute_terms(
        Resource.FOOD, want_qty, Resource.WATER, Bundle(water=99), urgency
    )

    assert give.total() / receive.total() <= MAX_PREMIUM_RATIO


@pytest.mark.parametrize("want_qty", range(1, 9))
def test_a_more_urgent_need_never_pays_less_per_unit(want_qty):
    """Urgency buys speed, so it must not round out to a cheaper rate."""

    def rate(urgency):
        give, receive = compute_terms(
            Resource.FOOD, want_qty, Resource.WATER, Bundle(water=99), urgency
        )
        return give.total() / receive.total()

    assert rate(Urgency.CRITICAL) >= rate(Urgency.WATCH) >= rate(Urgency.NONE)


def test_a_calm_single_unit_trade_is_not_rounded_up_to_double():
    """Ceiling a one-unit trade would pay a 100% premium for no urgency at all."""
    give, receive = compute_terms(
        Resource.FOOD, 1, Resource.WATER, Bundle(water=10), Urgency.WATCH
    )

    assert give.total() / receive.total() < MAX_PREMIUM_RATIO


def test_an_unaffordable_trade_is_scaled_down_rather_than_overpromised():
    """Posting locks nothing, so an offer must still be payable when accepted."""
    give, receive = compute_terms(
        Resource.FOOD, 4, Resource.WATER, Bundle(water=2), Urgency.NONE
    )

    assert give == Bundle(water=2)
    assert receive == Bundle(food=2)


def test_offering_a_resource_we_have_none_of_is_refused():
    with pytest.raises(CannotAfford):
        compute_terms(Resource.FOOD, 2, Resource.WATER, Bundle.zero(), Urgency.NONE)


def test_the_same_resource_may_not_appear_on_both_sides():
    with pytest.raises(CannotAfford, match="both sides"):
        compute_terms(Resource.WATER, 2, Resource.WATER, Bundle(water=9), Urgency.NONE)


def test_asking_for_nothing_is_refused():
    with pytest.raises(CannotAfford):
        compute_terms(Resource.FOOD, 0, Resource.WATER, Bundle(water=9), Urgency.NONE)


def test_trade_sizes_stay_small_so_one_failure_strands_little():
    assert desired_quantity(100) == MAX_TRADE_SIZE
    assert desired_quantity(1) == 1
    assert desired_quantity(0) == 1
