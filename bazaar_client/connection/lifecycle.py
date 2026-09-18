"""Connection lifecycle as a pure state machine.

Holds no socket, so the handshake, phase gating and reconnect rules can be
tested with synthetic events. `app.py` performs whatever this returns.

Sequence per the field manual: connect, read the first state, declare readiness,
wait for the matching confirmation, then trade only while the phase is RUNNING.
Readiness is required on every connection, including reconnects.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from bazaar_client.domain.types import (
    ControlCode,
    Phase,
    ProtocolErrorEvent,
    ReadinessAck,
    ServerEvent,
    Snapshot,
)

# Retrying these with the same token, version or run id cannot succeed.
FATAL_CONTROL_CODES = frozenset(
    {
        ControlCode.UNSUPPORTED_VERSION,
        ControlCode.RUN_MISMATCH,
        ControlCode.INVALID_AUTHENTICATION,
    }
)

TRADING_PHASES = frozenset({Phase.RUNNING})
# New trading actions stop here, though the connection stays readable.
TERMINAL_PHASES = frozenset({Phase.FINISHED, Phase.ABORTED})


class SessionState(enum.Enum):
    DISCONNECTED = "disconnected"
    AWAITING_FIRST_STATE = "awaiting_first_state"
    AWAITING_READINESS = "awaiting_readiness"
    ACTIVE = "active"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SendReady:
    """Declare readiness for this connection's first snapshot."""

    run_id: str
    snapshot_sequence: int


@dataclass(frozen=True, slots=True)
class Reconnect:
    reason: str


@dataclass(frozen=True, slots=True)
class Abort:
    reason: str
    code: ControlCode | None = None


Directive = SendReady | Reconnect | Abort


class RunIdChanged(RuntimeError):
    """Raised when a reconnect lands on a different exercise than before."""


class SessionLifecycle:
    def __init__(self) -> None:
        self._state = SessionState.DISCONNECTED
        self._run_id: str | None = None
        self._phase: Phase | None = None
        self._ready_sequence: int | None = None
        self._last_snapshot_sequence = 0

    # --- observable state -------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def phase(self) -> Phase | None:
        return self._phase

    @property
    def is_ready(self) -> bool:
        return self._state is SessionState.ACTIVE

    def can_send_trading_commands(self) -> bool:
        """Trading needs both a confirmed readiness and a RUNNING phase."""
        return self._state is SessionState.ACTIVE and self._phase in TRADING_PHASES

    def can_send_control_messages(self) -> bool:
        """`sync` stays available even before readiness is confirmed."""
        return self._state in {
            SessionState.AWAITING_FIRST_STATE,
            SessionState.AWAITING_READINESS,
            SessionState.ACTIVE,
        }

    # --- transitions ------------------------------------------------------

    def on_connected(self) -> None:
        self._state = SessionState.AWAITING_FIRST_STATE
        self._ready_sequence = None
        self._last_snapshot_sequence = 0

    def on_disconnected(self) -> None:
        if self._state is not SessionState.CLOSED:
            self._state = SessionState.DISCONNECTED

    def reset_for_new_connection(self) -> None:
        """A new connection restarts snapshot_sequence at 1 and needs a fresh Ready."""
        self._state = SessionState.DISCONNECTED
        self._phase = None
        self._ready_sequence = None
        self._last_snapshot_sequence = 0

    def on_event(self, event: ServerEvent) -> Directive | None:
        if isinstance(event, Snapshot):
            return self._on_snapshot(event)
        if isinstance(event, ReadinessAck):
            return self._on_readiness(event)
        if isinstance(event, ProtocolErrorEvent):
            return self._on_protocol_error(event)
        return None

    def _on_snapshot(self, snapshot: Snapshot) -> Directive | None:
        if self._run_id is not None and snapshot.run_id != self._run_id:
            raise RunIdChanged(
                f"connected to run {snapshot.run_id}, previously {self._run_id}; "
                "discard pending commands and object ids"
            )

        self._run_id = snapshot.run_id
        self._phase = snapshot.phase
        self._last_snapshot_sequence = max(
            self._last_snapshot_sequence, snapshot.snapshot_sequence
        )

        if self._state is SessionState.AWAITING_FIRST_STATE:
            self._state = SessionState.AWAITING_READINESS
            self._ready_sequence = snapshot.snapshot_sequence
            return SendReady(snapshot.run_id, snapshot.snapshot_sequence)
        return None

    def _on_readiness(self, ack: ReadinessAck) -> Directive | None:
        if self._state is not SessionState.AWAITING_READINESS:
            return None
        if ack.run_id != self._run_id:
            return Abort(f"readiness for run {ack.run_id}, expected {self._run_id}")
        if ack.snapshot_sequence != self._ready_sequence:
            return Abort(
                f"readiness acknowledged sequence {ack.snapshot_sequence}, "
                f"declared {self._ready_sequence}"
            )
        if not ack.ready:
            # The server agrees we are not ready; trading stays blocked.
            return None

        self._state = SessionState.ACTIVE
        return None

    def _on_protocol_error(self, event: ProtocolErrorEvent) -> Directive | None:
        if event.code in FATAL_CONTROL_CODES:
            self._state = SessionState.CLOSED
            return Abort(f"fatal control error {event.code.name}", event.code)
        if event.close_session:
            self._state = SessionState.DISCONNECTED
            return Reconnect(f"server closed session after {event.code.name}")
        return None
