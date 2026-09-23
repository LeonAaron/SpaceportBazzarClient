"""Does our policy make the world survive longer, and does our planet last longest?

Every world here is a full 120-tick run under the conditions of the
Directorate's run 2: nine planets, 30 of every resource at the start, and
production cycling 2/5/6 in 12-tick phases. The other planets are played by
stand-ins for the behaviours actually seen in that run (see strategies.py):
a client that never connected, a greedy build, small one-for-one traders, a
client that quit at tick 45, and a planet that gave its stock away.

Two measures:

  lifetime      the tick a planet failed, or RUN_TICKS + 1 if it never did, so a
                planet that survives outranks one that fails on the final tick
  world score   planet-ticks lived across all nine planets (at most 9 x 120)

What these tests cannot show: how eight other teams' real clients behave. The
stand-ins are modelled on run 2, and the next class run is the real test.
"""

from __future__ import annotations

import pytest

from bazaar_client.domain.types import Resource
from tests.survival.strategies import (
    RUN_TICKS,
    Gifter,
    Greedy,
    OurPolicy,
    Passive,
    Quitter,
    SmallFair,
    WorldOutcome,
    lineup,
    run_two_opponents,
    run_world,
)

PHASE_OFFSETS = (0, 1, 2)
MAX_WORLD_SCORE = 9 * RUN_TICKS


def lifetime(outcome: WorldOutcome, station_id: str) -> int:
    return outcome.failed_at.get(station_id, RUN_TICKS + 1)


def best_other_lifetime(outcome: WorldOutcome, us: str) -> int:
    return max(lifetime(outcome, sid) for sid in outcome.roster if sid != us)


# --- the world survives longer ------------------------------------------------


def test_a_world_running_our_policy_keeps_every_planet_alive_at_full_health():
    """The class goal: nine planets, all 120 ticks, nobody fails."""
    outcome = run_world([OurPolicy() for _ in range(9)])

    assert outcome.survivors == set(outcome.roster), outcome.summary()
    assert outcome.world_alive_ticks == MAX_WORLD_SCORE
    assert all(s.health == 100 for s in outcome.economy.stations.values()), outcome.summary()


@pytest.mark.parametrize("phase_offset", PHASE_OFFSETS)
def test_replacing_the_run_two_build_with_our_policy_lengthens_the_worlds_life(phase_offset):
    """Same eight opponents; only P01's client changes. Everyone benefits."""
    greedy = Greedy()
    before = run_world(lineup(greedy, run_two_opponents()), phase_offset=phase_offset)
    ours = OurPolicy()
    after = run_world(lineup(ours, run_two_opponents()), phase_offset=phase_offset)

    assert after.world_alive_ticks > before.world_alive_ticks, (before.summary(), after.summary())
    assert len(after.survivors) > len(before.survivors)
    assert before.station_of(greedy) in before.failed_at
    assert after.station_of(ours) in after.survivors


@pytest.mark.parametrize("phase_offset", PHASE_OFFSETS)
def test_every_planet_that_adopts_our_policy_leaves_the_world_better_off(phase_offset):
    """k planets switch from their run-2 behaviour to ours, the greedy build first.

    Any adoption beats the run-2 field, and full adoption beats every mix.
    Adding one more adopter is not guaranteed to help at every step: a planet
    that stops sharing its stock with a strained market can briefly cost a
    neighbour, so the test does not claim a strictly rising line.
    """
    scores = []
    for adopters in range(10):
        field = [Greedy()] + run_two_opponents()
        field = [OurPolicy() if i < adopters else s for i, s in enumerate(field)]
        scores.append(run_world(field, phase_offset=phase_offset).world_alive_ticks)

    assert all(score > scores[0] for score in scores[1:]), scores
    assert scores[-1] == max(scores) == MAX_WORLD_SCORE, scores
    assert scores[-1] > scores[len(scores) // 2] > scores[1], scores


def test_trading_our_way_beats_a_market_where_nobody_trades():
    idle = run_world([Passive() for _ in range(9)])
    ours = run_world(lineup(OurPolicy(), run_two_opponents()))

    assert idle.survivors == set()
    assert ours.world_alive_ticks > idle.world_alive_ticks


# --- our planet is the longest surviving -------------------------------------


@pytest.mark.parametrize("phase_offset", PHASE_OFFSETS)
@pytest.mark.parametrize("slot", range(9))
def test_our_planet_outlives_every_other_planet_in_the_run_two_field(slot, phase_offset):
    """Wherever we sit -- any specialty, any partners, any production phase --
    no planet lasts longer or ends healthier than ours."""
    ours = OurPolicy()
    outcome = run_world(lineup(ours, run_two_opponents(), slot), phase_offset=phase_offset)
    us = outcome.station_of(ours)
    stations = outcome.economy.stations

    assert us in outcome.survivors, outcome.summary()
    assert lifetime(outcome, us) >= best_other_lifetime(outcome, us), outcome.summary()
    assert stations[us].health >= max(
        stations[sid].health for sid in outcome.roster if sid != us
    ), outcome.summary()


@pytest.mark.parametrize("slot", range(3))
@pytest.mark.parametrize("rival", [Greedy, Gifter, Quitter], ids=lambda r: r.__name__)
def test_our_planet_strictly_outlives_eight_copies_of_a_weaker_client(rival, slot):
    """Against greedy, giving-away and quitting clients we are the last planet
    standing, whichever specialty we produce."""
    ours = OurPolicy()
    outcome = run_world(lineup(ours, [rival() for _ in range(8)], slot))
    us = outcome.station_of(ours)

    assert lifetime(outcome, us) > best_other_lifetime(outcome, us), outcome.summary()


@pytest.mark.parametrize("slot", range(3))
def test_among_fair_traders_everyone_survives_including_us(slot):
    """Fair partners are not rivals: the right outcome is a shared survival."""
    ours = OurPolicy()
    outcome = run_world(lineup(ours, [SmallFair() for _ in range(8)], slot))

    assert outcome.survivors == set(outcome.roster), outcome.summary()


def test_when_nobody_else_trades_we_last_as_long_as_anyone():
    """Survival needs imports, and imports need a partner. With none, no policy
    can do better than holding out as long as the planets around us."""
    ours = OurPolicy()
    outcome = run_world(lineup(ours, [Passive() for _ in range(8)]))
    us = outcome.station_of(ours)

    assert lifetime(outcome, us) >= best_other_lifetime(outcome, us)


@pytest.mark.parametrize("phase_offset", PHASE_OFFSETS)
def test_we_outlast_our_suppliers_going_quiet(phase_offset):
    """Run 2's real killer: partners stopping at tick 45. We keep enough of each
    import on hand to outlive every one of them."""
    ours = OurPolicy()
    outcome = run_world(lineup(ours, [Quitter() for _ in range(8)]), phase_offset=phase_offset)
    us = outcome.station_of(ours)

    assert lifetime(outcome, us) >= best_other_lifetime(outcome, us) + 20, outcome.summary()


def test_every_trade_in_every_world_stays_one_for_one_on_our_side():
    """The survival gains do not come from bending the trading rules."""
    ours = OurPolicy()
    outcome = run_world(lineup(ours, run_two_opponents()))
    us = outcome.station_of(ours)
    specialty = outcome.economy.stations[us].specialty

    for txn in outcome.economy.transactions:
        if us not in (txn.proposer_id, txn.recipient_id):
            continue
        paid = txn.give if txn.proposer_id == us else txn.receive
        got = txn.receive if txn.proposer_id == us else txn.give
        assert {r for r in Resource if paid.get(r)} <= {specialty}, txn
        assert paid.total() <= got.total() or got.is_zero(), txn
