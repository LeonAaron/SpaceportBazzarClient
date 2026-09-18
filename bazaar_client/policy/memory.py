"""What the policy carries between decisions.

Everything here is derived from snapshots we were sent. It holds estimates and
pending intent; server facts stay in the snapshot itself.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field

from bazaar_client.domain.types import Resource, Snapshot
from bazaar_client.world.counterparties import CounterpartyModel

ROUND_TRIP_SAMPLES = 8
PRODUCTION_SAMPLES = 8


@dataclass
class PolicyMemory:
    """Rolling estimates. `observe` folds each new snapshot in."""

    counterparties: CounterpartyModel = field(default_factory=CounterpartyModel)
    round_trips: deque[int] = field(default_factory=lambda: deque(maxlen=ROUND_TRIP_SAMPLES))
    productions: deque[int] = field(default_factory=lambda: deque(maxlen=PRODUCTION_SAMPLES))
    blocked_until_tick: int = 0
    altruism_log: dict[tuple[str, Resource], int] = field(default_factory=dict)

    _measured_transactions: set[str] = field(default_factory=set)
    _last_tick_seen: int = -1

    def observe(self, snapshot: Snapshot) -> "PolicyMemory":
        """Fold a snapshot into the rolling estimates and return self."""
        self.counterparties.update(snapshot)
        self._measure_round_trips(snapshot)
        self._measure_production(snapshot)
        return self

    def _measure_round_trips(self, snapshot: Snapshot) -> None:
        """How long our own offers take to settle, measured rather than assumed."""
        created: dict[str, int] = {o.offer_id: o.created_tick for o in snapshot.offers}
        for txn in snapshot.transactions:
            if txn.proposer_id != snapshot.self_station_id:
                continue
            if txn.transaction_id in self._measured_transactions:
                continue
            if txn.offer_id not in created:
                continue
            self._measured_transactions.add(txn.transaction_id)
            self.round_trips.append(max(0, txn.settled_tick - created[txn.offer_id]))

    def _measure_production(self, snapshot: Snapshot) -> None:
        """last_production reports what arrived, and only once per tick."""
        if snapshot.tick == self._last_tick_seen or snapshot.tick == 0:
            return
        self._last_tick_seen = snapshot.tick
        self.productions.append(snapshot.me.last_production.get(snapshot.me.specialty))

    def median_round_trip(self) -> int | None:
        if len(self.round_trips) < 3:
            return None
        return round(statistics.median(self.round_trips))

    def min_recent_production(self) -> int | None:
        return min(self.productions) if self.productions else None

    def record_gift(self, station_id: str, resource: Resource, tick: int) -> None:
        self.altruism_log[(station_id, resource)] = tick

    def ticks_since_gift(self, station_id: str, resource: Resource, tick: int) -> int:
        last = self.altruism_log.get((station_id, resource))
        return tick - last if last is not None else 10**6

    def block_until(self, tick: int | None) -> None:
        if tick is not None:
            self.blocked_until_tick = max(self.blocked_until_tick, tick)
