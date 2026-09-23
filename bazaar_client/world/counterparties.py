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
    # Resources this station has actually handed us: the strongest evidence of
    # what it produces, since specialties are never disclosed.
    supplied: set[Resource] = field(default_factory=set)
    # Whether it has ever shown signs of a running client. A planet whose client
    # never connected will ignore every offer we send it.
    ever_active: bool = False
    consecutive_expired: int = 0
    last_expired_tick: int = -1
    # The largest trade this partner seems able to take, learned from our own
    # offers: halved when one lapses, doubled when one is accepted. None until
    # an offer tells us something. A partner short of stock cannot accept a
    # large request however willing it is.
    size_limit: int | None = None

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
        self._update_supply(snapshot)

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
            stats.ever_active = True
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
            if offer.recipient_id == snapshot.self_station_id:
                # Proposing to us proves a live client, whatever its terms.
                self._stations.setdefault(
                    offer.proposer_id, CounterpartyStats(offer.proposer_id)
                ).ever_active = True
                continue
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
            asked = offer.receive.total()
            if offer.status is OfferStatus.ACCEPTED:
                stats.offers_accepted_by += 1
                stats.consecutive_expired = 0
                if stats.size_limit is not None:
                    stats.size_limit *= 2
            else:
                stats.consecutive_expired += 1
                stats.last_expired_tick = max(
                    stats.last_expired_tick,
                    offer.closed_tick if offer.closed_tick is not None else offer.expires_tick,
                )
                if asked:  # a declined gift says nothing about trade size
                    stats.size_limit = max(1, asked // 2)

    def _update_supply(self, snapshot: Snapshot) -> None:
        """Record what each partner has handed us in a settled trade."""
        me = snapshot.self_station_id
        for txn in snapshot.transactions:
            if me == txn.recipient_id:
                partner, received = txn.proposer_id, txn.give
            elif me == txn.proposer_id:
                partner, received = txn.recipient_id, txn.receive
            else:
                continue
            stats = self._stations.setdefault(partner, CounterpartyStats(partner))
            stats.ever_active = True
            stats.supplied.update(r for r in Resource if received.get(r) > 0)
