"""The authoritative view of the world.

A snapshot replaces the previous view wholesale. Its inventory already includes
every settled trade, so transactions are never re-applied on top of it, and a
duplicate or out-of-order snapshot must not roll the view backwards.
"""

from __future__ import annotations

import logging

from bazaar_client.domain.types import Phase, Snapshot

logger = logging.getLogger(__name__)


class WorldModel:
    def __init__(self) -> None:
        self._snapshot: Snapshot | None = None
        self._last_sequence = 0
        self._applied = 0
        self._ignored = 0

    @property
    def current(self) -> Snapshot | None:
        return self._snapshot

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def applied_count(self) -> int:
        return self._applied

    @property
    def ignored_count(self) -> int:
        return self._ignored

    def apply(self, snapshot: Snapshot) -> bool:
        """Replace the view. Returns False for a duplicate or stale snapshot.

        snapshot_sequence orders snapshots on one connection, so it -- not
        world_version -- decides whether a snapshot is newer: a fresh snapshot
        can carry new results while world_version is unchanged.
        """
        if self._snapshot is not None and snapshot.snapshot_sequence <= self._last_sequence:
            self._ignored += 1
            logger.debug(
                "ignoring snapshot %d; already at %d",
                snapshot.snapshot_sequence,
                self._last_sequence,
            )
            return False

        self._snapshot = snapshot
        self._last_sequence = snapshot.snapshot_sequence
        self._applied += 1
        return True

    def reset_for_new_connection(self) -> None:
        """A new connection restarts snapshot_sequence at 1."""
        self._snapshot = None
        self._last_sequence = 0

    # --- convenience reads ------------------------------------------------

    def require(self) -> Snapshot:
        if self._snapshot is None:
            raise RuntimeError("no snapshot has been applied yet")
        return self._snapshot

    @property
    def tick(self) -> int:
        return self.require().tick

    @property
    def phase(self) -> Phase:
        return self.require().phase

    def transaction_ids(self) -> frozenset[str]:
        return frozenset(t.transaction_id for t in self.require().transactions)
