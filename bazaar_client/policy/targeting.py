"""Who to approach, and with what.

Specialties and inventories are never disclosed, so counterparties are ranked
purely on what they have advertised, how recently, and how reliably they have
accepted our offers before. Advertised availability is a claim, not proof.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.policy.reserves import Urgency
from bazaar_client.world.counterparties import CounterpartyModel, CounterpartyStats

RECENCY_WINDOW = 10

WEIGHT_SELLS_WHAT_WE_NEED = 3.0
WEIGHT_SEEKS_WHAT_WE_OFFER = 2.0
WEIGHT_RECENCY = 1.0
WEIGHT_RELIABILITY = 1.0


@dataclass(frozen=True, slots=True)
class Candidate:
    station_id: str
    want: Resource
    give: Resource
    score: float


def score_counterparty(
    stats: CounterpartyStats, want: Resource, give: Resource, tick: int
) -> float:
    if stats.station_failed:
        return -math.inf

    recency = 0.0
    if stats.last_ad_tick >= 0:
        recency = max(0.0, 1.0 - (tick - stats.last_ad_tick) / RECENCY_WINDOW)

    return (
        WEIGHT_SELLS_WHAT_WE_NEED * (want in stats.selling)
        + WEIGHT_SEEKS_WHAT_WE_OFFER * (give in stats.seeking)
        + WEIGHT_RECENCY * recency
        + WEIGHT_RELIABILITY * stats.accept_rate
    )


def rank_counterparties(
    model: CounterpartyModel,
    urgency: dict[Resource, Urgency],
    wanted: dict[Resource, int],
    surplus: Bundle,
    tick: int,
    already_pending: frozenset[tuple[str, Resource]] = frozenset(),
) -> list[Candidate]:
    """One best candidate per resource we want, most urgent first.

    `already_pending` holds (station, resource) pairs we have an open offer for.
    Proposing the same trade again would promise the same stock twice and spend
    budget for nothing, since the first offer is still standing.
    """
    needs = [r for r, qty in wanted.items() if qty > 0]
    needs.sort(key=lambda r: (-int(urgency[r]), -wanted[r]))

    payable = [r for r in Resource if surplus.get(r) > 0]
    stations = model.stations()
    if not payable or not stations:
        return []

    candidates: list[Candidate] = []
    for want in needs:
        options = [
            (stats, give)
            for stats in stations
            for give in payable
            if give != want  # never put the same resource on both sides
            and (stats.station_id, want) not in already_pending
        ]
        if not options:
            continue
        stats, give = max(
            options, key=lambda pair: score_counterparty(pair[0], want, pair[1], tick)
        )
        best = score_counterparty(stats, want, give, tick)
        if best == -math.inf:
            continue
        candidates.append(Candidate(stats.station_id, want, give, best))

    return candidates
