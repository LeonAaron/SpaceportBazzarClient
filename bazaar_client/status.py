"""Where the client is, from "process running" to "actively participating".

Each step up the ladder needs evidence the previous one could not give:

  starting       the process is running; no connection attempted yet
  connecting     opening the WebSocket
  connected      socket open and subprotocol confirmed; nothing received yet
  authenticated  the server accepted our token and sent our first state
  synchronized   readiness confirmed for this connection's state
  waiting        synchronized, but the phase is READY or PAUSED: idle by design
  participating  RUNNING, receiving states and deciding every tick
  stale          RUNNING, but no new state for longer than the tick length allows
  finished       the run ended
  disconnected   the connection is gone; a reconnect may follow

"waiting" and "stale" are the two kinds of quiet, and telling them apart is the
point: a lobby can be silent for minutes, but a running game that stops
sending states means our view is out of date and decisions made from it are not
trustworthy.
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Callable

from bazaar_client.domain.types import Phase, Rules

logger = logging.getLogger(__name__)

MIN_STALE_AFTER_S = 2.0
STALE_TICKS = 3


class ClientStatus(enum.Enum):
    STARTING = "starting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    SYNCHRONIZED = "synchronized"
    WAITING = "waiting"
    PARTICIPATING = "participating"
    STALE = "stale"
    FINISHED = "finished"
    DISCONNECTED = "disconnected"


def stale_after_s(rules: Rules | None) -> float:
    """Three ticks without a state is stale; never less than two seconds."""
    if rules is None or rules.tick_duration_ms <= 0:
        return 15.0
    return max(MIN_STALE_AFTER_S, STALE_TICKS * rules.tick_duration_ms / 1000)


class StatusTracker:
    """Records status transitions and how long was spent in each."""

    def __init__(
        self,
        on_change: Callable[[ClientStatus, ClientStatus, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._on_change = on_change
        self._status = ClientStatus.STARTING
        self._since = clock()
        self._time_in: dict[ClientStatus, float] = {}
        self._last_state_at: float | None = None
        self._phase: Phase | None = None
        self._rules: Rules | None = None

    @property
    def current(self) -> ClientStatus:
        return self._status

    @property
    def stale_after_s(self) -> float:
        return stale_after_s(self._rules)

    @property
    def poll_interval_s(self) -> float:
        """How long to wait for a state before checking whether we are stale."""
        return max(0.5, self.stale_after_s / 2)

    def set(self, status: ClientStatus, detail: str = "") -> None:
        if status is self._status:
            return
        now = self._clock()
        self._time_in[self._status] = self._time_in.get(self._status, 0.0) + now - self._since
        previous, self._status, self._since = self._status, status, now
        logger.info("status %s -> %s%s", previous.value, status.value, f" ({detail})" if detail else "")
        if self._on_change is not None:
            self._on_change(previous, status, detail)

    def on_state(self, phase: Phase, rules: Rules, tick: int) -> None:
        """A new state arrived: we are synchronized and the phase says what to do."""
        self._last_state_at = self._clock()
        self._phase, self._rules = phase, rules
        if phase is Phase.RUNNING:
            self.set(ClientStatus.PARTICIPATING, f"tick {tick}")
        elif phase in (Phase.FINISHED, Phase.ABORTED):
            self.set(ClientStatus.FINISHED, phase.name)
        else:
            self.set(ClientStatus.WAITING, f"phase {phase.name}; the run has not started or is paused")

    def check_stale(self) -> bool:
        """Called when no state arrived in a while. Only a RUNNING game can be stale."""
        if self._phase is not Phase.RUNNING or self._last_state_at is None:
            return False
        silent = self._clock() - self._last_state_at
        if silent < self.stale_after_s:
            return False
        self.set(ClientStatus.STALE, f"no new state for {silent:.1f}s while RUNNING")
        return True

    def durations(self) -> dict[str, float]:
        """Seconds spent in each status, including the current one so far."""
        totals = dict(self._time_in)
        totals[self._status] = totals.get(self._status, 0.0) + self._clock() - self._since
        return {status.value: round(seconds, 3) for status, seconds in totals.items()}
