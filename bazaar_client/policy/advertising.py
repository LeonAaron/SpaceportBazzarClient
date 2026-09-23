"""What to publish about ourselves.

The advertisement is a stable, honest signal: we sell what we produce and seek
the two resources we consume but cannot make. Both are true every tick of the
run, so the ad rarely changes -- partners can rely on it, and we do not spend
commands republishing it. It is renewed before expiry, and only changes if we
run out of specialty to spare.
"""

from __future__ import annotations

from dataclasses import dataclass

from bazaar_client.domain.types import Advertisement, Resource, Rules
from bazaar_client.execution.actions import AdvertiseAction

AD_RENEW_MARGIN = 2
# A just-published ad is not replaced for a flicker in spare stock.
MIN_AD_LIFETIME = 3


@dataclass(frozen=True, slots=True)
class AdvertiseDecision:
    action: AdvertiseAction | None
    reason: str


def decide_advertisement(
    specialty: Resource,
    spendable: int,
    current: Advertisement | None,
    tick: int,
    rules: Rules,
) -> AdvertiseDecision:
    selling = frozenset({specialty}) if spendable >= 1 else frozenset()
    seeking = frozenset(r for r in Resource if r != specialty)
    ttl = min(rules.max_publication_ttl_ticks, rules.duration_ticks - tick)
    if ttl <= 0:
        return AdvertiseDecision(None, "no publication lifetime remains")
    proposed = AdvertiseAction(selling, seeking, tick + ttl)

    if current is None:
        return AdvertiseDecision(proposed, "no active advertisement")

    changed = (selling, seeking) != (current.selling, current.seeking)
    if changed and tick - current.created_tick >= MIN_AD_LIFETIME:
        return AdvertiseDecision(proposed, "what we can spare changed")

    if current.expires_tick - tick <= AD_RENEW_MARGIN:
        return AdvertiseDecision(proposed, "renewing before expiry")

    return AdvertiseDecision(None, "current advertisement still accurate")
