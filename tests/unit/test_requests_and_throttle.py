"""Request identity and the per-tick command budget."""

from __future__ import annotations

import pytest

from bazaar_client.connection.requests import (
    PendingRequestTracker,
    RequestIdConflictError,
    RequestIdGenerator,
    body_fingerprint,
    is_valid_request_id,
)
from bazaar_client.connection.throttle import CommandThrottle


# --- request ids ----------------------------------------------------------


def test_generated_ids_match_the_schema_charset():
    generator = RequestIdGenerator("P01")

    for kind in ("advertise", "offer", "accept", "withdraw"):
        assert is_valid_request_id(generator.next(kind))


def test_generated_ids_are_unique_per_command():
    generator = RequestIdGenerator("P01")
    ids = {generator.next("advertise") for _ in range(50)}

    assert len(ids) == 50


def test_generated_ids_stay_within_64_characters():
    generator = RequestIdGenerator("a-very-long-station-identifier" * 3)

    assert len(generator.next("advertise" * 5)) <= 64


def test_ids_are_sanitised_of_illegal_characters():
    generator = RequestIdGenerator("P01/../x", prefix="run id!")

    assert is_valid_request_id(generator.next("adv@ertise"))


@pytest.mark.parametrize(
    "candidate,valid",
    [("a", True), ("A-1_b", True), ("x" * 64, True), ("", False), ("x" * 65, False), ("bad id", False), ("bad.id", False)],
)
def test_request_id_validation_matches_the_stated_rule(candidate, valid):
    assert is_valid_request_id(candidate) is valid


# --- retry semantics ------------------------------------------------------


def test_resending_the_same_command_with_its_id_is_an_exact_retry():
    tracker = PendingRequestTracker()
    fingerprint = body_fingerprint(b"advertise-payload")
    tracker.register("req-1", fingerprint, "advertise", sent_tick=0)

    assert tracker.is_exact_retry("req-1", fingerprint)


def test_reusing_an_id_for_different_content_is_refused_locally():
    """Catching this here avoids earning RESULT_CODE_REQUEST_ID_CONFLICT."""
    tracker = PendingRequestTracker()
    tracker.register("req-1", body_fingerprint(b"first"), "advertise", 0)

    assert not tracker.is_exact_retry("req-1", body_fingerprint(b"second"))
    with pytest.raises(RequestIdConflictError, match="use a new id"):
        tracker.register("req-1", body_fingerprint(b"second"), "advertise", 0)


def test_an_unknown_id_is_not_a_retry():
    assert not PendingRequestTracker().is_exact_retry("never-sent", "abc")


def test_a_command_is_outstanding_until_its_answer_arrives():
    tracker = PendingRequestTracker()
    tracker.register("req-1", "fp", "offer", 0)
    assert tracker.is_outstanding("req-1")

    tracker.resolve("req-1")

    assert not tracker.is_outstanding("req-1")
    assert tracker.outstanding == frozenset()


def test_resolving_returns_what_the_id_was_used_for():
    tracker = PendingRequestTracker()
    tracker.register("req-1", "fp", "withdraw", sent_tick=4)

    pending = tracker.resolve("req-1")

    assert pending.kind == "withdraw"
    assert pending.sent_tick == 4


def test_fingerprints_differ_for_different_payloads():
    assert body_fingerprint(b"a") != body_fingerprint(b"b")
    assert body_fingerprint(b"a") == body_fingerprint(b"a")


# --- throttle -------------------------------------------------------------


def test_budget_comes_from_the_rules_limit():
    throttle = CommandThrottle(limit_per_tick=3)

    assert throttle.remaining(tick=0) == 3


def test_sending_consumes_the_budget():
    throttle = CommandThrottle(3)
    throttle.record_sent(0)
    throttle.record_sent(0)

    assert throttle.remaining(0) == 1


def test_budget_refreshes_on_a_new_tick():
    throttle = CommandThrottle(2)
    throttle.record_sent(0)
    throttle.record_sent(0)
    assert throttle.remaining(0) == 0

    assert throttle.remaining(1) == 2


def test_budget_never_goes_negative():
    throttle = CommandThrottle(1)
    throttle.record_sent(0, count=5)

    assert throttle.remaining(0) == 0


def test_limit_updates_when_the_rules_change():
    """Rules are read from each snapshot rather than baked in."""
    throttle = CommandThrottle(2)
    throttle.update_limit(5)

    assert throttle.remaining(0) == 5


def test_rate_limit_blocks_every_command_until_retry_after_tick():
    throttle = CommandThrottle(4)
    throttle.block_until(7)

    assert throttle.remaining(5) == 0
    assert throttle.remaining(6) == 0
    assert throttle.remaining(7) == 4


def test_block_until_ignores_an_absent_retry_after_tick():
    throttle = CommandThrottle(2)
    throttle.block_until(None)

    assert throttle.remaining(0) == 2


def test_block_until_never_shortens_an_existing_block():
    throttle = CommandThrottle(2)
    throttle.block_until(10)
    throttle.block_until(3)

    assert throttle.blocked_until_tick == 10
