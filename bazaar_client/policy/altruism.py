"""Helping a planet that looks like it is in trouble.

The class wins only if none of the nine planets ever reaches zero health, and
that failure is permanent and collective. Our own specialty regenerates every
tick and piles up far beyond what trades absorb, so a gift from that pile costs
us nothing we need -- and a planet we keep alive stays a trading partner.

Distress can only be inferred from public signals: health and inventories are
private, so an advertisement that keeps seeking our specialty is the evidence.

Gifts are bounded so generosity can never become our own shortage: only our
specialty, never an imported resource; only once every import is at target;
only from stock well above what we trade with; a fraction of it, capped; and
with a cooldown per partner.
"""

from __future__ import annotations

from dataclasses import dataclass

from bazaar_client.domain.types import Resource
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.world.counterparties import CounterpartyModel

PERSISTENCE_TICKS = 4
DONATION_CAP_FRACTION = 0.2
MAX_GIFT_SIZE = 10
COOLDOWN_TICKS = 5
MAX_GIFTS_PER_TICK = 1


@dataclass(frozen=True, slots=True)
class GiftIntent:
    station_id: str
    resource: Resource
    quantity: int


def donation_size(excess: int) -> int:
    """A bounded fraction of idle stock, at least one unit to be useful."""
    return max(1, min(int(excess * DONATION_CAP_FRACTION), MAX_GIFT_SIZE))


def scan_for_distress(
    model: CounterpartyModel,
    specialty: Resource,
    excess: int,
    tick: int,
    memory: PolicyMemory,
) -> list[GiftIntent]:
    """`excess` is specialty stock above the floor we keep for trading."""
    if excess <= 0:
        return []

    intents: list[GiftIntent] = []
    for stats in model.stations():
        if stats.seeking_streak.get(specialty, 0) < PERSISTENCE_TICKS:
            continue
        if memory.ticks_since_gift(stats.station_id, specialty, tick) < COOLDOWN_TICKS:
            continue
        intents.append(GiftIntent(stats.station_id, specialty, donation_size(excess)))

    # Longest-standing need first; it is the strongest evidence of real trouble.
    intents.sort(key=lambda g: -model.get(g.station_id).seeking_streak.get(specialty, 0))
    return intents[:MAX_GIFTS_PER_TICK]
