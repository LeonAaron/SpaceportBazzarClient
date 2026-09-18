"""What we can infer about the other planets.

Their inventories, health and specialties are never disclosed, so this is built
only from public advertisements and from trades we were party to. Advertised
availability is a claim, not proof of stock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bazaar_client.domain.types import (
    OfferStatus,
    PublicationStatus,
    Resource,
    Snapshot,
)

NEUTRAL_ACCEPT_RATE = 0.5
RECIPIENT_DECIDED = frozenset({OfferStatus.ACCEPTED, OfferStatus.EXPIRED})


@dataclass
class CounterpartyStats:
    station_id: str
    display_name: str = ""
    selling: frozenset[Resource] = frozenset()
    seeking: frozenset[Resource] = frozenset()
    seeking_streak: dict[Resource, int] = field(default_factory=dict)
    last_ad_tick: int = -1
    offers_sent_to: int = 0
    offers_accepted_by: int = 0
    station_failed: bool = False

    @property
    def accept_rate(self) -> float:
        if not self.offers_sent_to:
            return NEUTRAL_ACCEPT_RATE
        return self.offers_accepted_by / self.offers_sent_to


class CounterpartyModel:
    def __init__(self) -> None:
        self._stations: dict[str, CounterpartyStats] = {}
        self._counted_offers: set[str] = set()

    def stations(self) -> list[CounterpartyStats]:
        return [s for s in self._stations.values() if not s.station_failed]

    def get(self, station_id: str) -> CounterpartyStats | None:
        return self._stations.get(station_id)

    def mark_failed(self, station_id: str) -> None:
        """A station that lost trading eligibility never regains it."""
        stats = self._stations.get(station_id)
        if stats is not None:
            stats.station_failed = True

    def update(self, snapshot: Snapshot) -> None:
        self._update_directory(snapshot)
        self._update_advertisements(snapshot)
        self._update_offer_history(snapshot)

    def _update_directory(self, snapshot: Snapshot) -> None:
        for entry in snapshot.directory:
            if entry.station_id == snapshot.self_station_id:
                continue
            stats = self._stations.setdefault(
                entry.station_id, CounterpartyStats(entry.station_id)
            )
            stats.display_name = entry.display_name

    def _update_advertisements(self, snapshot: Snapshot) -> None:
        active: dict[str, tuple[frozenset[Resource], frozenset[Resource]]] = {}
        for ad in snapshot.advertisements:
            if ad.station_id == snapshot.self_station_id:
                continue
            if ad.status is not PublicationStatus.ACTIVE:
                continue
            if ad.is_expired_at(snapshot.tick):
                continue
            active[ad.station_id] = (ad.selling, ad.seeking)

        for station_id, (selling, seeking) in active.items():
            stats = self._stations.setdefault(
                station_id, CounterpartyStats(station_id)
            )
            # The server emits a snapshot for every world change, so several can
            # share one tick; only a later tick counts as the need persisting.
            is_new_tick = snapshot.tick > stats.last_ad_tick
            stats.selling = selling
            stats.seeking = seeking
            stats.last_ad_tick = snapshot.tick
            if is_new_tick:
                for resource in seeking:
                    stats.seeking_streak[resource] = stats.seeking_streak.get(resource, 0) + 1
            for resource in list(stats.seeking_streak):
                if resource not in seeking:
                    del stats.seeking_streak[resource]

        for station_id, stats in self._stations.items():
            if station_id not in active:
                stats.selling = frozenset()
                stats.seeking = frozenset()
                stats.seeking_streak.clear()

    def _update_offer_history(self, snapshot: Snapshot) -> None:
        """Count each of our offers once, when it reaches a settled status."""
        for offer in snapshot.offers:
            if offer.proposer_id != snapshot.self_station_id:
                continue
            # Only outcomes the recipient decided count. A withdrawal is our own
            # choice, and a run ending is nobody's, so neither says anything
            # about how reliably this station accepts.
            if offer.status not in RECIPIENT_DECIDED or offer.offer_id in self._counted_offers:
                continue

            self._counted_offers.add(offer.offer_id)
            stats = self._stations.setdefault(
                offer.recipient_id, CounterpartyStats(offer.recipient_id)
            )
            stats.offers_sent_to += 1
            if offer.status is OfferStatus.ACCEPTED:
                stats.offers_accepted_by += 1
