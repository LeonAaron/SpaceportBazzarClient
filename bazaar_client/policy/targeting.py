"""Who to approach for each resource we need.

Specialties and inventories are never disclosed, so we rank partners on the
evidence we do have: what they advertise selling, what they have actually handed
us before, whether they seek what we produce, and how they treat our offers.

We always pay with our own specialty, so the question is only who is likely to
have what we want and to accept it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from bazaar_client.domain.types import Resource
from bazaar_client.policy.pricing import TRADE_SIZE_MAX
from bazaar_client.world.counterparties import CounterpartyModel, CounterpartyStats

RECENCY_WINDOW = 10

WEIGHT_SELLS_WHAT_WE_NEED = 4.0
WEIGHT_HAS_SUPPLIED_IT = 3.0
WEIGHT_SEEKS_WHAT_WE_OFFER = 2.0
WEIGHT_RECENCY = 1.0
WEIGHT_RELIABILITY = 1.0

# A lapsed offer first shrinks what we ask that partner for (see
# CounterpartyStats.size_limit). Only when even one-unit offers keep lapsing is
# the partner rested, so commands go to someone who might answer. Resting a
# partner that is merely short of stock would cut off a scarce supplier.
BACKOFF_AFTER_EXPIRIES = 2
BACKOFF_TICKS = 6

# Parallel offers for one resource, each to a different partner. Overfilling is
# harmless because we only ever pay with surplus of what we produce.
MAX_OFFERS_PER_NEED = 2


@dataclass(frozen=True, slots=True)
class Candidate:
    station_id: str
    want: Resource
    give: Resource
    score: float
    size_limit: int = TRADE_SIZE_MAX


def size_limit_for(stats: CounterpartyStats) -> int:
    if stats.size_limit is None:
        return TRADE_SIZE_MAX
    return max(1, min(stats.size_limit, TRADE_SIZE_MAX))


def is_backed_off(stats: CounterpartyStats, tick: int) -> bool:
    return (
        size_limit_for(stats) == 1
        and stats.consecutive_expired >= BACKOFF_AFTER_EXPIRIES
        and tick - stats.last_expired_tick < BACKOFF_TICKS
    )


def has_evidence_of(stats: CounterpartyStats, want: Resource) -> bool:
    return want in stats.selling or want in stats.supplied


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
        + WEIGHT_HAS_SUPPLIED_IT * (want in stats.supplied)
        + WEIGHT_SEEKS_WHAT_WE_OFFER * (give in stats.seeking)
        + WEIGHT_RECENCY * recency
        + WEIGHT_RELIABILITY * stats.accept_rate
    )


def _partners_for(
    stations: list[CounterpartyStats], want: Resource, give: Resource, tick: int
) -> list[CounterpartyStats]:
    """Best first. Planets with evidence of producing `want` crowd out guesses."""
    eligible = [s for s in stations if not s.station_failed and not is_backed_off(s, tick)]
    active = [s for s in eligible if s.ever_active] or eligible
    likely = [s for s in active if has_evidence_of(s, want)] or active
    return sorted(likely, key=lambda s: -score_counterparty(s, want, give, tick))


def rank_counterparties(
    model: CounterpartyModel,
    specialty: Resource,
    wanted: dict[Resource, int],
    tick: int,
    already_pending: frozenset[tuple[str, Resource]] = frozenset(),
) -> list[Candidate]:
    """Partners to approach, every need's first choice before any second choice.

    `already_pending` holds (station, resource) pairs we have an open offer for;
    proposing the same trade again would promise the same stock twice.
    """
    needs = sorted(
        (r for r, qty in wanted.items() if qty > 0 and r != specialty),
        key=lambda r: -wanted[r],
    )
    stations = model.stations()

    per_need: dict[Resource, list[Candidate]] = {}
    for want in needs:
        open_for_want = sum(1 for _, r in already_pending if r == want)
        room = MAX_OFFERS_PER_NEED - open_for_want
        if room <= 0:
            continue
        per_need[want] = [
            Candidate(
                s.station_id, want, specialty,
                score_counterparty(s, want, specialty, tick), size_limit_for(s),
            )
            for s in _partners_for(stations, want, specialty, tick)
            if (s.station_id, want) not in already_pending
        ][:room]

    ranked: list[Candidate] = []
    for position in range(MAX_OFFERS_PER_NEED):
        for want in needs:
            choices = per_need.get(want, [])
            if position < len(choices):
                ranked.append(choices[position])
    return ranked
