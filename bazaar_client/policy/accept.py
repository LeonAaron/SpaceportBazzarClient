"""Whether to settle an offer addressed to us.

Only the recipient can accept, and there is no reject command: declining just
means not accepting, and the offer later expires or is withdrawn.

The rule mirrors how we propose: we pay only with the resource we produce, never
more than we receive, and only for something we cannot make ourselves. Such a
trade is good for both sides -- we turn regenerating stock into upkeep we would
otherwise lack, and the proposer gets the resource it is short of.
"""

from __future__ import annotations

from dataclasses import dataclass

from bazaar_client.domain.types import Offer, Resource


@dataclass(frozen=True, slots=True)
class AcceptDecision:
    accept: bool
    reason: str


def evaluate_incoming(
    offer: Offer,
    station_id: str,
    specialty: Resource,
    spendable: int,
) -> AcceptDecision:
    cost = offer.what_station_pays(station_id)
    gain = offer.what_station_receives(station_id)

    if cost.is_zero():
        # A gift: it can only help, and the protocol still requires an accept.
        return AcceptDecision(True, "gift")

    if any(cost.get(r) > 0 for r in Resource if r != specialty):
        return AcceptDecision(False, "would pay with a resource we cannot produce")

    if cost.total() > gain.total():
        return AcceptDecision(False, "asks more than it gives")

    if cost.get(specialty) > spendable:
        return AcceptDecision(False, "cannot pay without dipping into our reserve")

    if not any(gain.get(r) > 0 for r in Resource if r != specialty):
        return AcceptDecision(False, "brings nothing we need to import")

    return AcceptDecision(True, "our specialty for what we import, at least 1:1")
