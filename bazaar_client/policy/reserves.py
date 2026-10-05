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


WATCH_STOCK = 20
CRITICAL_STOCK = 10
PRODUCTION_NORMAL_TICKS = 25
PRODUCTION_STOP_TICKS = 10
STORAGE_THRESHOLD = 32
MAX_RESERVE_UNITS = 15

# Imports are only useful in balance: health falls when any one runs out, so
# piling up one while the other drains buys nothing. An import may lead the
# other by this much before we stop buying more of it.
BALANCE_GAP = 10
IMPORT_CEILING_FLOOR = 2 * WATCH_STOCK
# Unspent production is worth nothing once the run ends, so in the last ticks
# the specialty only needs to cover the upkeep that remains.
END_GAME_TICKS = 10
# Stock past this is hoarding: every planet consumes the same upkeep, so units
# beyond it help us far less than they would help a planet that is asking.
STOCK_CAP = 100


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
    rules: Rules, station: StationObservation, memory: PolicyMemory,
    available: Bundle | None = None,
) -> Bundle:
    """Bank excess above 32, without counting already stored stock again.

    Storage is local protection inside inventory, not a server-side transfer.
    The upkeep buffer plus saved surplus cannot exceed 15 per resource.
    """
    available = station.inventory if available is None else available
    reserves = []
    stored = []
    for resource in Resource:
        base = min(MAX_RESERVE_UNITS, station.upkeep_per_tick.get(resource)
                   * safety_ticks(resource, rules, station, memory))
        have = available.get(resource)
        saved = min(memory.stored_reserve.get(resource), have, MAX_RESERVE_UNITS - base)
        excess = max(0, have - saved - STORAGE_THRESHOLD)
        saved = min(MAX_RESERVE_UNITS - base, saved + excess)
        stored.append(saved)
        reserves.append(base + saved)
    memory.stored_reserve = Bundle(*stored)
    return Bundle(*reserves)


def compute_urgency(
    available: Bundle, upkeep: Bundle, reserve: Bundle
) -> dict[Resource, Urgency]:
    """Escalate below 20 and 10 uncommitted units, or sooner for high upkeep."""
    urgency: dict[Resource, Urgency] = {}
    for resource in Resource:
        have = available.get(resource)
        if have < max(CRITICAL_STOCK, upkeep.get(resource)):
            urgency[resource] = Urgency.CRITICAL
        elif have < max(WATCH_STOCK, reserve.get(resource)):
            urgency[resource] = Urgency.WATCH
        else:
            urgency[resource] = Urgency.NONE
    return urgency


def surplus_above_reserve(available: Bundle, reserve: Bundle) -> Bundle:
    return available.saturating_sub(reserve)


def deficit_below_reserve(available: Bundle, reserve: Bundle) -> Bundle:
    return reserve.saturating_sub(available)


def in_end_game(ticks_left: int | None) -> bool:
    return ticks_left is not None and ticks_left <= END_GAME_TICKS


def specialty_floor(station: StationObservation, ticks_left: int | None = None) -> int:
    """Specialty stock to keep back: the normal floor, or what upkeep remains."""
    upkeep = station.upkeep_per_tick.get(station.specialty)
    if in_end_game(ticks_left):
        return min(PRODUCTION_NORMAL_TICKS, ticks_left + 1) * upkeep
    return PRODUCTION_NORMAL_TICKS * upkeep


def production_payment_limit(
    available: Bundle, station: StationObservation, ticks_left: int | None = None
) -> float | None:
    """Cap payments by uncommitted ticks of our produced resource's upkeep."""
    upkeep = station.upkeep_per_tick.get(station.specialty)
    stock = available.get(station.specialty)
    if in_end_game(ticks_left):
        return 0.0 if stock < specialty_floor(station, ticks_left) else None
    if stock < PRODUCTION_STOP_TICKS * upkeep:
        return 0.0
    if stock < PRODUCTION_NORMAL_TICKS * upkeep:
        return 0.5
    return None


def imports(specialty: Resource | None) -> list[Resource]:
    return [r for r in Resource if r != specialty]


def import_ceiling(resource: Resource, available: Bundle, specialty: Resource | None) -> int | None:
    """Most of an import worth holding: a little ahead of the scarcest other import."""
    others = [available.get(r) for r in imports(specialty) if r != resource]
    if resource == specialty or not others:
        return None
    return max(IMPORT_CEILING_FLOOR, min(others) + BALANCE_GAP)


def over_ceiling(resource: Resource, available: Bundle, specialty: Resource | None) -> bool:
    ceiling = import_ceiling(resource, available, specialty)
    return ceiling is not None and available.get(resource) > ceiling


def scarcest_import(available: Bundle, specialty: Resource | None) -> Resource | None:
    candidates = imports(specialty)
    return min(candidates, key=lambda r: (available.get(r), r)) if candidates else None


def excess_over_cap(available: Bundle) -> Bundle:
    """Uncommitted stock above STOCK_CAP, which we give away rather than hold."""
    return Bundle(*(max(0, available.get(r) - STOCK_CAP) for r in Resource))


def comfortably_supplied(available: Bundle) -> bool:
    """All resources must be strictly above 30 uncommitted units."""
    return all(available.get(resource) > 30 for resource in Resource)
