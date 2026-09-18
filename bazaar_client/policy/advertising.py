"""What to publish about ourselves.

There is one active advertisement per planet and a new one replaces the previous
one, so the slot has to be spent carefully: replacing it every tick would burn
command budget and tell counterparties nothing new.
"""

from __future__ import annotations

from dataclasses import dataclass

from bazaar_client.domain.types import Advertisement, Bundle, Resource, Rules
from bazaar_client.execution.actions import AdvertiseAction, WithdrawAction

# A one-unit surplus is erased by a single trade, so it is not worth listing.
MIN_LISTABLE_QTY = 2
DEFAULT_AD_TTL = 8
AD_RENEW_MARGIN = 2
MIN_AD_LIFETIME = 3


@dataclass(frozen=True, slots=True)
class AdvertiseDecision:
    action: AdvertiseAction | WithdrawAction | None
    reason: str


def decide_advertisement(
    surplus: Bundle,
    deficit: Bundle,
    critical: frozenset[Resource],
    current: Advertisement | None,
    tick: int,
    rules: Rules,
) -> AdvertiseDecision:
    selling = frozenset(r for r in Resource if surplus.get(r) >= MIN_LISTABLE_QTY)
    seeking = frozenset(r for r in Resource if deficit.get(r) > 0 or r in critical)
    ttl = min(DEFAULT_AD_TTL, rules.max_publication_ttl_ticks)
    proposed = AdvertiseAction(selling, seeking, tick + ttl)

    if current is None:
        if not selling and not seeking:
            return AdvertiseDecision(None, "nothing to sell or seek")
        return AdvertiseDecision(proposed, "no active advertisement")

    if critical - current.seeking:
        # A new critical need outranks the churn guard.
        return AdvertiseDecision(proposed, "new critical need")

    if not selling and not seeking:
        return AdvertiseDecision(
            WithdrawAction(current.advertisement_id), "no longer selling or seeking"
        )

    changed = (selling, seeking) != (current.selling, current.seeking)
    if changed and tick - current.created_tick >= MIN_AD_LIFETIME:
        return AdvertiseDecision(proposed, "needs changed materially")

    if current.expires_tick - tick <= AD_RENEW_MARGIN:
        return AdvertiseDecision(proposed, "renewing before expiry")

    return AdvertiseDecision(None, "current advertisement still accurate")
