"""Connection lifecycle: handshake, phase gating, control errors, reconnect.

All synthetic events -- no socket -- so the rules are pinned down independently
of whether a practice server happens to be running.
"""

from __future__ import annotations

import pytest

from bazaar_client.connection.lifecycle import (
    Abort,
    Reconnect,
    RunIdChanged,
    SendReady,
    SessionLifecycle,
    SessionState,
)
from bazaar_client.domain.types import ControlCode, Phase
from tests.fixtures import factories


def connected_lifecycle() -> SessionLifecycle:
    lifecycle = SessionLifecycle()
    lifecycle.on_connected()
    return lifecycle


def make_ready(lifecycle: SessionLifecycle, sequence: int = 1, phase: Phase = Phase.RUNNING):
    snapshot = factories.make_snapshot(snapshot_sequence=sequence, phase=phase)
    lifecycle.on_event(snapshot)
    lifecycle.on_event(factories.make_readiness(snapshot_sequence=sequence))
    return snapshot


# --- the handshake --------------------------------------------------------


def test_first_state_triggers_a_readiness_declaration():
    lifecycle = connected_lifecycle()
    snapshot = factories.make_snapshot(snapshot_sequence=1)

    directive = lifecycle.on_event(snapshot)

    assert directive == SendReady(run_id=snapshot.run_id, snapshot_sequence=1)
    assert lifecycle.state is SessionState.AWAITING_READINESS


def test_matching_readiness_activates_the_session():
    lifecycle = connected_lifecycle()
    make_ready(lifecycle)

    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle.is_ready


def test_readiness_for_a_different_sequence_is_rejected():
    """The confirmation must acknowledge the snapshot we actually declared."""
    lifecycle = connected_lifecycle()
    lifecycle.on_event(factories.make_snapshot(snapshot_sequence=1))

    directive = lifecycle.on_event(factories.make_readiness(snapshot_sequence=7))

    assert isinstance(directive, Abort)
    assert lifecycle.state is not SessionState.ACTIVE


def test_readiness_for_a_different_run_is_rejected():
    lifecycle = connected_lifecycle()
    lifecycle.on_event(factories.make_snapshot(snapshot_sequence=1))

    directive = lifecycle.on_event(
        factories.make_readiness(run_id="some-other-run", snapshot_sequence=1)
    )

    assert isinstance(directive, Abort)


def test_ready_false_acknowledgement_leaves_trading_blocked():
    """`ready: false` reports we are not ready; it does not activate the session."""
    lifecycle = connected_lifecycle()
    lifecycle.on_event(factories.make_snapshot(snapshot_sequence=1))

    lifecycle.on_event(factories.make_readiness(ready=False, snapshot_sequence=1))

    assert lifecycle.state is SessionState.AWAITING_READINESS
    assert not lifecycle.can_send_trading_commands()


def test_later_snapshots_do_not_re_declare_readiness():
    lifecycle = connected_lifecycle()
    make_ready(lifecycle, sequence=1)

    assert lifecycle.on_event(factories.make_snapshot(snapshot_sequence=2)) is None


# --- phase gating ---------------------------------------------------------


def test_trading_requires_readiness_even_while_running():
    lifecycle = connected_lifecycle()
    lifecycle.on_event(factories.make_snapshot(snapshot_sequence=1, phase=Phase.RUNNING))

    assert not lifecycle.can_send_trading_commands()


@pytest.mark.parametrize(
    "phase,allowed",
    [
        (Phase.RUNNING, True),
        (Phase.READY, False),
        (Phase.PAUSED, False),
        (Phase.FINISHED, False),
        (Phase.ABORTED, False),
    ],
)
def test_trading_is_allowed_only_while_running(phase, allowed):
    lifecycle = connected_lifecycle()
    make_ready(lifecycle, phase=phase)

    assert lifecycle.can_send_trading_commands() is allowed


def test_phase_change_after_readiness_regates_trading():
    """A run that pauses mid-session must stop new trading actions."""
    lifecycle = connected_lifecycle()
    make_ready(lifecycle, sequence=1, phase=Phase.RUNNING)
    assert lifecycle.can_send_trading_commands()

    lifecycle.on_event(factories.make_snapshot(snapshot_sequence=2, phase=Phase.PAUSED))

    assert not lifecycle.can_send_trading_commands()


def test_control_messages_stay_available_before_readiness():
    """`sync` remains available while not ready."""
    lifecycle = connected_lifecycle()
    lifecycle.on_event(factories.make_snapshot(snapshot_sequence=1))

    assert lifecycle.can_send_control_messages()


# --- control errors -------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        ControlCode.UNSUPPORTED_VERSION,
        ControlCode.RUN_MISMATCH,
        ControlCode.INVALID_AUTHENTICATION,
    ],
)
def test_fatal_control_codes_abort_instead_of_retrying(code):
    """Retrying with the same token, version or run id cannot succeed."""
    lifecycle = connected_lifecycle()
    make_ready(lifecycle)

    directive = lifecycle.on_event(factories.make_protocol_error(code=code))

    assert isinstance(directive, Abort)
    assert directive.code is code
    assert lifecycle.state is SessionState.CLOSED


def test_close_session_triggers_a_reconnect():
    lifecycle = connected_lifecycle()
    make_ready(lifecycle)

    directive = lifecycle.on_event(
        factories.make_protocol_error(code=ControlCode.BAD_MESSAGE, close_session=True)
    )

    assert isinstance(directive, Reconnect)


def test_non_fatal_error_without_close_session_keeps_the_session():
    """README step 9: capacity exceeded, close_session false, connection stays open."""
    lifecycle = connected_lifecycle()
    make_ready(lifecycle)

    directive = lifecycle.on_event(
        factories.make_protocol_error(
            code=ControlCode.REQUEST_CAPACITY_EXCEEDED, close_session=False
        )
    )

    assert directive is None
    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle.can_send_trading_commands()


# --- reconnect ------------------------------------------------------------


def test_reconnect_requires_a_fresh_readiness_exchange():
    """Readiness is required on every connection, including reconnects."""
    lifecycle = connected_lifecycle()
    make_ready(lifecycle, sequence=6)
    assert lifecycle.is_ready

    lifecycle.reset_for_new_connection()
    lifecycle.on_connected()
    assert not lifecycle.is_ready

    directive = lifecycle.on_event(factories.make_snapshot(snapshot_sequence=1))

    assert isinstance(directive, SendReady)
    assert directive.snapshot_sequence == 1


def test_a_different_run_id_after_reconnect_is_surfaced():
    """A new exercise invalidates pending commands and object ids."""
    lifecycle = connected_lifecycle()
    make_ready(lifecycle)
    lifecycle.reset_for_new_connection()
    lifecycle.on_connected()

    with pytest.raises(RunIdChanged):
        lifecycle.on_event(factories.make_snapshot(run_id="a-different-run"))


def test_disconnect_leaves_the_session_reconnectable():
    lifecycle = connected_lifecycle()
    make_ready(lifecycle)

    lifecycle.on_disconnected()

    assert lifecycle.state is SessionState.DISCONNECTED
    assert not lifecycle.can_send_trading_commands()


def test_a_fatal_abort_is_not_undone_by_a_disconnect():
    lifecycle = connected_lifecycle()
    make_ready(lifecycle)
    lifecycle.on_event(
        factories.make_protocol_error(code=ControlCode.INVALID_AUTHENTICATION)
    )

    lifecycle.on_disconnected()

    assert lifecycle.state is SessionState.CLOSED
