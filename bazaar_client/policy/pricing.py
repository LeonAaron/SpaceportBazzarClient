"""What to ask for, and what to pay.

There is no currency, so a price is just the ratio between the two bundles.
Every planet consumes one unit of each resource per tick, so no resource is
worth more than another: we trade strictly one-for-one. Asking for more than we
give gets ignored by sensible peers, and paying more than we get only drains us.

Trades are sized to what we actually need, up to a large cap, so one settlement
covers many ticks of upkeep instead of each tick depending on a fresh trade.
"""

from __future__ import annotations

from bazaar_client.domain.types import Bundle, Resource

TRADE_SIZE_MAX = 20


class CannotAfford(ValueError):
    """Raised when there is nothing we can pay or nothing worth asking for."""


def size_trade(want_qty: int, spendable: int) -> int:
    """How many units to exchange: what we need, what we can pay, and the cap."""
    size = min(want_qty, spendable, TRADE_SIZE_MAX)
    if size <= 0:
        raise CannotAfford("nothing to ask for" if want_qty <= 0 else "nothing to pay with")
    return size


def compute_terms(
    want: Resource, want_qty: int, give_resource: Resource, spendable: int
) -> tuple[Bundle, Bundle]:
    """Returns (give, receive) from our perspective as the proposer, always 1:1."""
    if want == give_resource:
        raise CannotAfford("cannot put the same resource on both sides of an offer")
    quantity = size_trade(want_qty, spendable)
    return Bundle.single(give_resource, quantity), Bundle.single(want, quantity)
