"""Helping a planet that looks like it is in trouble.

The class wins only if none of the nine planets ever reaches zero health, and
that failure is permanent and collective. The prizes for prosperity are graded
and personal. So a small gift out of stock we already hold above our own reserve
buys down a shared, irreversible failure with a bounded, recoverable cost -- and
a planet we keep alive stays a trading partner.

Distress can only be inferred from public signals: health and inventories are
private, so a need repeated across ticks is the evidence available.

The caps exist so generosity can never become our own shortage: we give only
when nothing of ours is urgent, only from surplus above reserve, only a fraction
of it, only once per station and resource within a cooldown, and only with
command budget left over.
"""

from __future__ import annotations

from dataclasses import dataclass

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.reserves import Urgency
from bazaar_client.world.counterparties import CounterpartyModel

PERSISTENCE_TICKS = 4
DONATION_CAP_FRACTION = 0.2
COOLDOWN_TICKS = 5
MAX_GIFTS_PER_TICK = 1


@dataclass(frozen=True, slots=True)
class GiftIntent:
    station_id: str
    resource: Resource
    quantity: int


def donation_size(surplus_qty: int) -> int:
    """A bounded fraction of idle surplus, and at least one unit to be useful."""
    return max(1, int(surplus_qty * DONATION_CAP_FRACTION))


def scan_for_distress(
    model: CounterpartyModel,
    surplus: Bundle,
    urgency: dict[Resource, Urgency],
    tick: int,
    memory: PolicyMemory,
) -> list[GiftIntent]:
    if any(level is not Urgency.NONE for level in urgency.values()):
        return []  # protect ourselves completely first

    intents: list[GiftIntent] = []
    for stats in model.stations():
        for resource, streak in stats.seeking_streak.items():
            if streak < PERSISTENCE_TICKS:
                continue
            if surplus.get(resource) <= 0:
                continue
            if memory.ticks_since_gift(stats.station_id, resource, tick) < COOLDOWN_TICKS:
                continue
            intents.append(
                GiftIntent(stats.station_id, resource, donation_size(surplus.get(resource)))
            )

    # Longest-standing need first; it is the strongest evidence of real trouble.
    intents.sort(
        key=lambda g: -(model.get(g.station_id).seeking_streak.get(g.resource, 0))
    )
    return intents[:MAX_GIFTS_PER_TICK]
