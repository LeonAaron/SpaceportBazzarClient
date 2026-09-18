"""Whether to settle an offer addressed to us.

Only the recipient can accept, and there is no reject command: declining just
means not accepting, and the offer later expires or is withdrawn.

Acceptance is atomic, so the question is only whether the exchange leaves us
better off without breaking our upkeep reserve.
"""

from __future__ import annotations

from dataclasses import dataclass

from bazaar_client.domain.types import Bundle, Offer, Resource
from bazaar_client.policy.reserves import Urgency

# A trade can look slightly lossy in raw units and still be worth taking when it
# brings in something we actually need.
MIN_ACCEPT_RATIO = 0.8

WEIGHT_CRITICAL = 1.0
WEIGHT_WATCH = 0.3


@dataclass(frozen=True, slots=True)
class AcceptDecision:
    accept: bool
    reason: str


def need_weights(urgency: dict[Resource, Urgency]) -> dict[Resource, float]:
    weights = {}
    for resource, level in urgency.items():
        if level is Urgency.CRITICAL:
            weights[resource] = 1.0 + WEIGHT_CRITICAL
        elif level is Urgency.WATCH:
            weights[resource] = 1.0 + WEIGHT_WATCH
        else:
            weights[resource] = 1.0
    return weights


def weighted_value(bundle: Bundle, weights: dict[Resource, float]) -> float:
    return sum(bundle.get(r) * weights[r] for r in Resource)


def evaluate_incoming(
    offer: Offer,
    station_id: str,
    available: Bundle,
    reserve: Bundle,
    urgency: dict[Resource, Urgency],
) -> AcceptDecision:
    cost = offer.what_station_pays(station_id)
    gain = offer.what_station_receives(station_id)

    if cost.is_zero():
        # A gift: it can only help, and the protocol still requires an accept.
        return AcceptDecision(True, "gift")

    if not available.dominates(cost):
        # Accepting would fail on insufficient resources and settle nothing.
        return AcceptDecision(False, "cannot pay from uncommitted stock")

    fills_need = any(
        gain.get(r) > 0 and urgency[r] is not Urgency.NONE for r in Resource
    )
    remaining = available.saturating_sub(cost) + gain
    breaches_reserve = any(
        remaining.get(r) < reserve.get(r) < available.get(r) for r in Resource
    )

    weights = need_weights(urgency)
    cost_value = weighted_value(cost, weights)
    ratio = weighted_value(gain, weights) / cost_value if cost_value else 0.0

    if breaches_reserve and not fills_need:
        return AcceptDecision(False, "would breach reserve without filling a need")

    if fills_need and ratio >= MIN_ACCEPT_RATIO:
        return AcceptDecision(True, "fills a need at an acceptable price")

    if ratio >= 1.0:
        return AcceptDecision(True, "favourable terms")

    return AcceptDecision(False, f"price below threshold ({ratio:.2f})")
