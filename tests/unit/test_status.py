"""The status ladder, and telling a deliberate wait from a stale view."""

from __future__ import annotations

from bazaar_client.domain.types import Phase
from bazaar_client.status import ClientStatus, StatusTracker, stale_after_s
from tests.fixtures import factories


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def tracker():
    changes = []
    clock = Clock()
    status = StatusTracker(on_change=lambda old, new, why: changes.append((old, new)), clock=clock)
    return status, changes, clock


RULES = factories.make_rules(tick_duration_ms=1000)


def test_the_ladder_is_climbed_one_observable_step_at_a_time():
    status, changes, _ = tracker()
    for step in (ClientStatus.CONNECTING, ClientStatus.CONNECTED,
                 ClientStatus.AUTHENTICATED, ClientStatus.SYNCHRONIZED):
        status.set(step)
    status.on_state(Phase.RUNNING, RULES, tick=1)

    assert [new for _, new in changes] == [
        ClientStatus.CONNECTING, ClientStatus.CONNECTED, ClientStatus.AUTHENTICATED,
        ClientStatus.SYNCHRONIZED, ClientStatus.PARTICIPATING,
    ]


def test_repeating_a_status_is_not_a_transition():
    status, changes, _ = tracker()
    status.set(ClientStatus.CONNECTING)
    status.set(ClientStatus.CONNECTING)

    assert len(changes) == 1


def test_a_quiet_lobby_is_waiting_not_stale():
    status, _, clock = tracker()
    status.on_state(Phase.READY, RULES, tick=0)
    clock.now += 600  # ten minutes of silence before the run starts

    assert status.check_stale() is False
    assert status.current is ClientStatus.WAITING


def test_a_running_game_that_stops_sending_states_is_stale():
    status, _, clock = tracker()
    status.on_state(Phase.RUNNING, RULES, tick=5)
    clock.now += stale_after_s(RULES) - 0.1
    assert status.check_stale() is False

    clock.now += 0.2
    assert status.check_stale() is True
    assert status.current is ClientStatus.STALE

    status.on_state(Phase.RUNNING, RULES, tick=9)
    assert status.current is ClientStatus.PARTICIPATING


def test_stale_means_three_ticks_but_never_under_two_seconds():
    assert stale_after_s(factories.make_rules(tick_duration_ms=5000)) == 15.0
    assert stale_after_s(factories.make_rules(tick_duration_ms=100)) == 2.0
    assert stale_after_s(None) == 15.0


def test_a_finished_run_is_finished():
    status, _, _ = tracker()
    status.on_state(Phase.FINISHED, RULES, tick=120)

    assert status.current is ClientStatus.FINISHED


def test_time_is_attributed_to_each_status():
    status, _, clock = tracker()
    clock.now = 1.0
    status.set(ClientStatus.CONNECTING)
    clock.now = 1.5
    status.on_state(Phase.RUNNING, RULES, tick=0)
    clock.now = 11.5

    assert status.durations() == {"starting": 1.0, "connecting": 0.5, "participating": 10.0}
