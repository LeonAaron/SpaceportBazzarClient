"""How much we refuse to trade away.

Health is a hard constraint and zero health is permanent, so the first duty is
keeping our own upkeep covered. The reserve is held in ticks of upkeep, sized
from the trade latency we have actually observed rather than a guess, and read
from rules and upkeep_per_tick rather than hardcoded numbers.

Stock is measured as available-to-commit, not raw inventory: an open offer can
be accepted at any moment, so stock already promised cannot also feed upkeep.
"""

from __future__ import annotations

import enum

from bazaar_client.domain.types import Bundle, Resource, Rules, StationObservation
from bazaar_client.policy.memory import PolicyMemory

# One tick for our offer to reach a counterparty's snapshot, one for their
# acceptance to reach ours, one for scheduling slack.
MIN_SAFETY_TICKS = 3
MAX_SAFETY_TICKS = 10
LOW_HEALTH_EXTRA_TICKS = 2
PRODUCTION_VARIANCE_EXTRA_TICKS = 1

# Resources we cannot produce arrive only when a trade settles, and partners can
# go quiet at any time -- in run 2 most clients stopped trading around tick 45.
# So we aim to hold enough of each import to last the rest of the run if trading
# stopped now: at least IMPORT_TARGET_MIN_TICKS of upkeep, at most
# IMPORT_TARGET_MAX_TICKS. The cap was chosen in the mixed-opponent simulation
# (tests/survival/test_world_survival.py): higher caps hoarded supply other
# planets needed and lowered how long the world as a whole survived.
IMPORT_TARGET_MIN_TICKS = 20
IMPORT_TARGET_MAX_TICKS = 60


class Urgency(enum.IntEnum):
    NONE = 0
    WATCH = 1
    CRITICAL = 2


def safety_ticks(
    resource: Resource, rules: Rules, station: StationObservation, memory: PolicyMemory
) -> int:
    observed = memory.median_round_trip()
    base = MIN_SAFETY_TICKS if observed is None else max(MIN_SAFETY_TICKS, observed + 1)

    if station.health < rules.max_health // 2:
        base += LOW_HEALTH_EXTRA_TICKS

    if resource == station.specialty:
        lowest = memory.min_recent_production()
        if lowest is not None and lowest < station.upkeep_per_tick.get(resource):
            # Our own output has dipped below what we consume; stop assuming it
            # will cover us next tick.
            base += PRODUCTION_VARIANCE_EXTRA_TICKS

    return min(base, MAX_SAFETY_TICKS)


def compute_reserve(
    rules: Rules, station: StationObservation, memory: PolicyMemory
) -> Bundle:
    return Bundle(
        *(
            station.upkeep_per_tick.get(r) * safety_ticks(r, rules, station, memory)
            for r in Resource
        )
    )


def compute_urgency(
    available: Bundle, upkeep: Bundle, reserve: Bundle
) -> dict[Resource, Urgency]:
    """CRITICAL means the next tick's upkeep is not covered by uncommitted stock."""
    urgency: dict[Resource, Urgency] = {}
    for resource in Resource:
        have = available.get(resource)
        if have < upkeep.get(resource):
            urgency[resource] = Urgency.CRITICAL
        elif have < reserve.get(resource):
            urgency[resource] = Urgency.WATCH
        else:
            urgency[resource] = Urgency.NONE
    return urgency


def import_target_ticks(ticks_remaining: int) -> int:
    return max(IMPORT_TARGET_MIN_TICKS, min(IMPORT_TARGET_MAX_TICKS, ticks_remaining))


def import_target(station: StationObservation, reserve: Bundle, ticks_remaining: int) -> Bundle:
    """How much of each imported resource to hold; zero for our own specialty."""
    ticks = import_target_ticks(ticks_remaining)
    return Bundle(
        *(
            0
            if r == station.specialty
            else max(reserve.get(r), station.upkeep_per_tick.get(r) * ticks)
            for r in Resource
        )
    )


def specialty_spendable(available: Bundle, reserve: Bundle, specialty: Resource) -> int:
    """Specialty stock we can promise away: everything above its reserve."""
    return max(0, available.get(specialty) - reserve.get(specialty))


def surplus_above_reserve(available: Bundle, reserve: Bundle) -> Bundle:
    return available.saturating_sub(reserve)


def deficit_below_reserve(available: Bundle, reserve: Bundle) -> Bundle:
    return reserve.saturating_sub(available)
