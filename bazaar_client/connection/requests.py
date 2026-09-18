"""Request identity.

A request_id labels one command. Reusing an id is only correct when retrying
that exact command; reusing it with different content earns
RESULT_CODE_REQUEST_ID_CONFLICT, so ids are generated fresh per new intent.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from dataclasses import dataclass

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RequestIdConflictError(ValueError):
    """Raised when an id would be reused for different command content."""


def is_valid_request_id(request_id: str) -> bool:
    return bool(REQUEST_ID_PATTERN.match(request_id))


def body_fingerprint(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:16]


class RequestIdGenerator:
    """Produces ids within the schema's 1-64 character letters/digits/_/- charset."""

    def __init__(self, station_id: str, prefix: str = "") -> None:
        self._station_id = re.sub(r"[^A-Za-z0-9_-]", "", station_id) or "sta"
        self._prefix = re.sub(r"[^A-Za-z0-9_-]", "", prefix)
        self._counter = itertools.count(1)

    def next(self, kind: str) -> str:
        kind = re.sub(r"[^A-Za-z0-9_-]", "", kind) or "cmd"
        parts = [p for p in (self._prefix, kind, self._station_id, str(next(self._counter))) if p]
        request_id = "-".join(parts)[:64]
        assert is_valid_request_id(request_id), request_id
        return request_id


@dataclass(frozen=True, slots=True)
class PendingRequest:
    request_id: str
    fingerprint: str
    kind: str
    sent_tick: int


class PendingRequestTracker:
    """Remembers what each id was used for, so retries stay exact."""

    def __init__(self) -> None:
        self._sent: dict[str, PendingRequest] = {}
        self._open: set[str] = set()

    def register(self, request_id: str, fingerprint: str, kind: str, sent_tick: int) -> None:
        previous = self._sent.get(request_id)
        if previous and previous.fingerprint != fingerprint:
            raise RequestIdConflictError(
                f"request_id {request_id} was already used for different content; "
                "use a new id instead"
            )
        self._sent[request_id] = PendingRequest(request_id, fingerprint, kind, sent_tick)
        self._open.add(request_id)

    def is_exact_retry(self, request_id: str, fingerprint: str) -> bool:
        previous = self._sent.get(request_id)
        return previous is not None and previous.fingerprint == fingerprint

    def resolve(self, request_id: str) -> PendingRequest | None:
        self._open.discard(request_id)
        return self._sent.get(request_id)

    def is_outstanding(self, request_id: str) -> bool:
        return request_id in self._open

    @property
    def outstanding(self) -> frozenset[str]:
        return frozenset(self._open)
