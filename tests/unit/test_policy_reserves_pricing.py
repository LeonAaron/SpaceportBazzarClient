"""Reserves, urgency and pricing."""

from __future__ import annotations

import pytest

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.pricing import (
    TRADE_SIZE_MAX,
    CannotAfford,
    compute_terms,
    size_trade,
)
from bazaar_client.policy.reserves import (
    MAX_SAFETY_TICKS,
    MIN_SAFETY_TICKS,
    Urgency,
    compute_reserve,
    IMPORT_TARGET_MAX_TICKS,
    IMPORT_TARGET_MIN_TICKS,
    compute_urgency,
    deficit_below_reserve,
    import_target,
    safety_ticks,
    specialty_spendable,
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


# --- import targets -------------------------------------------------------


def test_imports_are_held_to_a_deep_target_and_our_specialty_to_none():
    station = factories.make_station(specialty=Resource.WATER)
    target = import_target(station, reserve=Bundle(3, 3, 3), ticks_remaining=100)

    assert target.water == 0
    assert target.food == IMPORT_TARGET_MAX_TICKS
    assert target.components == IMPORT_TARGET_MAX_TICKS


@pytest.mark.parametrize(
    "remaining, expected",
    [(120, IMPORT_TARGET_MAX_TICKS), (45, 45), (5, IMPORT_TARGET_MIN_TICKS)],
)
def test_the_import_target_covers_the_rest_of_the_run_within_bounds(remaining, expected):
    """Enough to outlast partners going quiet, without hoarding past the cap."""
    station = factories.make_station(specialty=Resource.WATER)

    assert import_target(station, Bundle(3, 3, 3), ticks_remaining=remaining).food == expected


def test_the_import_target_never_falls_below_the_reserve():
    station = factories.make_station(specialty=Resource.WATER)

    assert import_target(station, reserve=Bundle(3, 99, 3), ticks_remaining=100).food == 99


def test_spendable_specialty_is_what_lies_above_its_reserve():
    assert specialty_spendable(Bundle(water=10), Bundle(3, 3, 3), Resource.WATER) == 7
    assert specialty_spendable(Bundle(water=2), Bundle(3, 3, 3), Resource.WATER) == 0


# --- pricing --------------------------------------------------------------


@pytest.mark.parametrize("want_qty", [1, 3, 7, 20, 50])
@pytest.mark.parametrize("spendable", [1, 5, 20, 99])
def test_every_trade_is_exactly_one_for_one(want_qty, spendable):
    """No resource is worth more than another, so we never pay more than we get."""
    give, receive = compute_terms(Resource.FOOD, want_qty, Resource.WATER, spendable)

    assert give.total() == receive.total()
    assert give == Bundle(water=give.total())
    assert receive == Bundle(food=receive.total())


def test_a_trade_covers_the_whole_need_in_one_settlement():
    give, receive = compute_terms(Resource.FOOD, 15, Resource.WATER, spendable=40)

    assert give == Bundle(water=15)
    assert receive == Bundle(food=15)


def test_a_trade_is_capped_at_the_maximum_size():
    assert size_trade(want_qty=100, spendable=100) == TRADE_SIZE_MAX


def test_a_trade_is_scaled_down_to_what_we_can_pay_and_stays_one_for_one():
    """Posting locks nothing, so an offer must still be payable when accepted."""
    give, receive = compute_terms(Resource.FOOD, 12, Resource.WATER, spendable=5)

    assert give == Bundle(water=5)
    assert receive == Bundle(food=5)


def test_offering_when_nothing_is_spendable_is_refused():
    with pytest.raises(CannotAfford):
        compute_terms(Resource.FOOD, 2, Resource.WATER, spendable=0)


def test_the_same_resource_may_not_appear_on_both_sides():
    with pytest.raises(CannotAfford, match="both sides"):
        compute_terms(Resource.WATER, 2, Resource.WATER, spendable=9)


def test_asking_for_nothing_is_refused():
    with pytest.raises(CannotAfford):
        compute_terms(Resource.FOOD, 0, Resource.WATER, spendable=9)
