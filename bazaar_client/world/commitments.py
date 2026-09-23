"""What we can still promise.

Posting an offer checks our ability to pay but locks nothing, so several open
offers can promise the same stock. The server keeps no escrow; this ledger is
how we avoid overcommitting.

Two layers: open offers read straight from each snapshot (self-correcting, and
authoritative), plus a short-lived in-flight record covering the gap between
sending an offer and seeing it in the next snapshot.
"""

from __future__ import annotations

import logging

from bazaar_client.domain.types import Bundle, OfferStatus, Snapshot

logger = logging.getLogger(__name__)


class CommitmentTracker:
    def __init__(self) -> None:
        self._inflight: dict[str, Bundle] = {}

    def register_inflight(self, request_id: str, give: Bundle) -> None:
        """Record a just-sent offer's give side, before it reaches a snapshot."""
        self._inflight[request_id] = give

    def resolve_inflight(self, request_id: str) -> None:
        """Release after rejection or an authoritative confirming snapshot."""
        self._inflight.pop(request_id, None)

    def clear_inflight(self) -> None:
        self._inflight.clear()

    @property
    def inflight_total(self) -> Bundle:
        total = Bundle.zero()
        for give in self._inflight.values():
            total = total + give
        return total

    def outstanding_confirmed_give(self, snapshot: Snapshot) -> Bundle:
        """Sum of what our still-open offers have promised away."""
        total = Bundle.zero()
        for offer in snapshot.offers:
            if offer.proposer_id != snapshot.self_station_id:
                continue
            if offer.status is not OfferStatus.OPEN:
                continue
            if offer.is_expired_at(snapshot.tick):
                continue  # an expired offer cannot be accepted, so it frees its stock
            total = total + offer.give
        return total

    def available_to_commit(self, snapshot: Snapshot) -> Bundle:
        """Stock not already promised to an open or in-flight offer."""
        committed = self.outstanding_confirmed_give(snapshot)
        return (
            snapshot.me.inventory.saturating_sub(committed)
            .saturating_sub(self.inflight_total)
        )

    def can_afford(self, snapshot: Snapshot, cost: Bundle) -> bool:
        return self.available_to_commit(snapshot).dominates(cost)
