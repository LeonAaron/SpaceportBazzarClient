"""What to ask for, and what to pay.

There is no currency, so a price is just the ratio between the two bundles.
Every planet consumes all three resources, so one-for-one is the neutral
baseline; urgency buys speed by sweetening our side, within a hard cap so a bad
estimate can never give the planet away.
"""

from __future__ import annotations

import math

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.policy.reserves import Urgency

BASE_RATIO = 1.0
MAX_PREMIUM_RATIO = 8.0

# Small trades limit exposure; the 1.2 tier uses five to express six-for-five.
MAX_TRADE_SIZE = 4


class CannotAfford(ValueError):
    """Raised when we cannot pay for the quantity we want."""


def premium_for(urgency: Urgency, stock: int | None = None) -> float:
    """Pay modest premiums early, then compound desperation below seven units."""
    if stock is None:
        return {Urgency.NONE: BASE_RATIO, Urgency.WATCH: 1.2,
                Urgency.CRITICAL: 2.0}[urgency]
    if stock >= 20:
        return BASE_RATIO
    if stock >= 15:
        return 1.2
    if stock >= 10:
        return 1.5
    if stock >= 7:
        return 2.0 + 0.25 * (9 - stock)
    return min(2.5 * 1.25 ** (7 - max(0, stock)), MAX_PREMIUM_RATIO)


def desired_quantity(deficit: int, stock: int | None = None) -> int:
    """Ask for enough to clear the shortfall, capped to keep trades small."""
    cap = 5 if stock is not None and 15 <= stock < 20 else MAX_TRADE_SIZE
    return max(1, min(deficit, cap))


def priced_give_quantity(want_qty: int, urgency: Urgency, stock: int | None = None) -> int:
    """Round the stock-based price half-up, keeping the effective rate capped."""
    priced = math.floor(want_qty * premium_for(urgency, stock) + 0.5)
    capped = math.floor(want_qty * MAX_PREMIUM_RATIO)
    return max(want_qty, min(priced, capped))


def compute_terms(
    want: Resource,
    want_qty: int,
    give_resource: Resource,
    available: Bundle,
    urgency: Urgency,
    *,
    max_payment_ratio: float | None = None,
    stock: int | None = None,
) -> tuple[Bundle, Bundle]:
    """Returns (give, receive) from our perspective as the proposer."""
    if want == give_resource:
        raise CannotAfford("cannot put the same resource on both sides of an offer")
    if want_qty <= 0:
        raise CannotAfford("nothing to ask for")

    affordable = available.get(give_resource)
    if affordable <= 0:
        raise CannotAfford(f"no {give_resource.name} available to offer")

    if max_payment_ratio is not None:
        # Floor keeps small, indivisible trades within the supply protection cap.
        give_qty = min(affordable, math.floor(want_qty * max_payment_ratio))
        if give_qty <= 0:
            raise CannotAfford("production supply guard prevents paid trade")
        return Bundle.single(give_resource, give_qty), Bundle.single(want, want_qty)

    give_qty = priced_give_quantity(want_qty, urgency, stock)

    if give_qty > affordable:
        # Scale the whole trade down rather than promising stock we do not hold.
        give_qty = affordable
        want_qty = max(1, math.floor(give_qty / premium_for(urgency, stock)))
        give_qty = min(give_qty, priced_give_quantity(want_qty, urgency, stock))

    return Bundle.single(give_resource, give_qty), Bundle.single(want, want_qty)