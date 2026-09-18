"""Chosen actions, independent of the wire.

The decision layer returns these; the executor turns them into commands. They
carry no protobuf, so policy stays testable without a socket.
"""

from __future__ import annotations

from dataclasses import dataclass

from bazaar_client.domain.types import Bundle, Resource


@dataclass(frozen=True, slots=True)
class AdvertiseAction:
    selling: frozenset[Resource]
    seeking: frozenset[Resource]
    expires_tick: int
    kind: str = "advertise"

    def describe(self) -> dict:
        return {
            "selling": sorted(r.name for r in self.selling),
            "seeking": sorted(r.name for r in self.seeking),
            "expires_tick": self.expires_tick,
        }


@dataclass(frozen=True, slots=True)
class OfferAction:
    recipient_id: str
    give: Bundle
    receive: Bundle
    expires_tick: int
    kind: str = "offer"

    @property
    def is_gift(self) -> bool:
        return self.receive.is_zero()

    def describe(self) -> dict:
        return {
            "recipient_id": self.recipient_id,
            "give": self.give.as_dict(),
            "receive": self.receive.as_dict(),
            "expires_tick": self.expires_tick,
            "is_gift": self.is_gift,
        }


@dataclass(frozen=True, slots=True)
class AcceptAction:
    offer_id: str
    kind: str = "accept"

    def describe(self) -> dict:
        return {"offer_id": self.offer_id}


@dataclass(frozen=True, slots=True)
class WithdrawAction:
    object_id: str
    kind: str = "withdraw"

    def describe(self) -> dict:
        return {"object_id": self.object_id}


@dataclass(frozen=True, slots=True)
class SyncAction:
    """A control message: it asks for a fresh snapshot and advances no time."""

    kind: str = "sync"

    def describe(self) -> dict:
        return {}


Action = AdvertiseAction | OfferAction | AcceptAction | WithdrawAction | SyncAction
TRADING_ACTIONS = (AdvertiseAction, OfferAction, AcceptAction, WithdrawAction)
