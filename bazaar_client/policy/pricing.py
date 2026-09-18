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
PREMIUM_STEP = 0.25
MAX_PREMIUM_RATIO = 2.0

# Small, frequent trades: one failed acceptance should not strand a large offer.
MAX_TRADE_SIZE = 4


class CannotAfford(ValueError):
    """Raised when we cannot pay for the quantity we want."""


def premium_for(urgency: Urgency) -> float:
    return min(BASE_RATIO + PREMIUM_STEP * int(urgency), MAX_PREMIUM_RATIO)


def desired_quantity(deficit: int) -> int:
    """Ask for enough to clear the shortfall, capped to keep trades small."""
    return max(1, min(deficit, MAX_TRADE_SIZE))


def priced_give_quantity(want_qty: int, urgency: Urgency) -> int:
    """How much to hand over for `want_qty`, rounded half-up and capped.

    Rounding up unconditionally would overpay badly on small trades -- a single
    unit has no way to express a 25% premium, and ceiling it means paying double
    for a need that is not urgent at all. Half-up keeps the effective rate close
    to the premium urgency actually justifies, and the cap is enforced on the
    result so rounding can never push us past it.
    """
    priced = math.floor(want_qty * premium_for(urgency) + 0.5)
    capped = math.floor(want_qty * MAX_PREMIUM_RATIO)
    return max(want_qty, min(priced, capped))


def compute_terms(
    want: Resource,
    want_qty: int,
    give_resource: Resource,
    available: Bundle,
    urgency: Urgency,
) -> tuple[Bundle, Bundle]:
    """Returns (give, receive) from our perspective as the proposer."""
    if want == give_resource:
        raise CannotAfford("cannot put the same resource on both sides of an offer")
    if want_qty <= 0:
        raise CannotAfford("nothing to ask for")

    affordable = available.get(give_resource)
    if affordable <= 0:
        raise CannotAfford(f"no {give_resource.name} available to offer")

    give_qty = priced_give_quantity(want_qty, urgency)

    if give_qty > affordable:
        # Scale the whole trade down rather than promising stock we do not hold.
        give_qty = affordable
        want_qty = max(1, math.floor(give_qty / premium_for(urgency)))

    return Bundle.single(give_resource, give_qty), Bundle.single(want, want_qty)
