"""Executor: command construction, commitment bookkeeping and evidence.

Driven through a fake session so failure paths a healthy practice server never
produces (timeouts, refusals, lost connections) can be exercised directly.
"""

from __future__ import annotations

import asyncio

import pytest

from bazaar_client.app import CommandOutcome
from bazaar_client.domain.types import Bundle, ControlCode, OfferStatus, Resource, ResultCode
from bazaar_client.execution.actions import (
    AcceptAction,
    AdvertiseAction,
    OfferAction,
    SyncAction,
    WithdrawAction,
)
from bazaar_client.execution.evidence import EvidenceLog
from bazaar_client.execution.executor import Executor
from bazaar_client.world.commitments import CommitmentTracker
from bazaar_client.world.counterparties import CounterpartyModel
from tests.fixtures import factories


class FakeSession:
    def __init__(self, outcome=None, error=None) -> None:
        self.latest_snapshot = factories.make_snapshot(snapshot_sequence=3, tick=2)
        self.run_id = factories.RUN_ID
        self.sent = []
        self._outcome = outcome
        self._error = error
        self.on_send = None

    async def send_command(self, message, kind, request_id, timeout=15.0):
        self.sent.append((kind, request_id, message))
        if self.on_send is not None:
            self.on_send()
        if self._error is not None:
            raise self._error
        return self._outcome or CommandOutcome(
            request_id, result=factories.make_result(request_id=request_id)
        )

    async def wait_for_result_snapshot(self, result, timeout=15.0):
        self.latest_snapshot = factories.make_snapshot(
            snapshot_sequence=4, world_version=5, request_results=(result,)
        )
        return self.latest_snapshot

    async def send_sync(self):
        self.sent.append(("sync", None, None))


def make_executor(session=None, **kwargs):
    session = session or FakeSession()
    evidence = EvidenceLog()
    commitments = CommitmentTracker()
    counterparties = CounterpartyModel()
    executor = Executor(session, evidence, commitments, counterparties)
    return executor, session, evidence, commitments, counterparties


OFFER = OfferAction("P02", Bundle(water=2), Bundle(food=1), expires_tick=6)


# --- command construction -------------------------------------------------


@pytest.mark.parametrize(
    "action,arm",
    [
        (AdvertiseAction(frozenset({Resource.WATER}), frozenset(), 6), "advertise"),
        (OFFER, "offer"),
        (AcceptAction("offer-1"), "accept"),
        (WithdrawAction("ad-1"), "withdraw"),
    ],
)
async def test_each_action_builds_its_matching_command(action, arm):
    executor, session, *_ = make_executor()

    await executor.execute(action, "req-1")

    _, request_id, message = session.sent[0]
    assert request_id == "req-1"
    assert message.WhichOneof("message") == arm


def test_sync_is_a_control_message_not_a_command():
    executor, *_ = make_executor()

    with pytest.raises(TypeError, match="not a command action"):
        executor.build(SyncAction(), factories.RUN_ID, "req-1")


# --- commitments ----------------------------------------------------------


async def test_an_offers_stock_is_held_while_it_is_in_flight():
    """Between sending and seeing the offer in a snapshot, nothing else may spend it."""
    executor, session, _, commitments, _ = make_executor()
    seen = []
    session.on_send = lambda: seen.append(commitments.inflight_total)

    await executor.execute(OFFER, "req-1")

    assert seen == [Bundle(water=2)]


async def test_the_hold_is_released_once_the_confirming_snapshot_arrives():
    executor, _, _, commitments, _ = make_executor()

    await executor.execute(OFFER, "req-1")

    assert commitments.inflight_total == Bundle.zero()


async def test_a_timed_out_send_keeps_stock_reserved_until_reconnect():
    """The server may have accepted an offer whose answer was lost."""
    executor, _, _, commitments, _ = make_executor(FakeSession(error=asyncio.TimeoutError()))

    with pytest.raises(asyncio.TimeoutError):
        await executor.execute(OFFER, "req-1")

    assert commitments.inflight_total == OFFER.give


async def test_a_failed_send_still_leaves_evidence_of_the_attempt():
    executor, _, evidence, _, _ = make_executor(FakeSession(error=ConnectionError("lost")))

    with pytest.raises(ConnectionError):
        await executor.execute(OFFER, "req-1", step="4")

    record = evidence.records[0]
    assert record.step == "4"
    assert record.request_id == "req-1"
    assert any("no answer" in note for note in record.notes)


async def test_non_offer_commands_reserve_nothing():
    executor, session, _, commitments, _ = make_executor()
    session.on_send = lambda: None
    seen = []
    session.on_send = lambda: seen.append(commitments.inflight_total)

    await executor.execute(AcceptAction("offer-1"), "req-1")

    assert seen == [Bundle.zero()]


# --- recording outcomes ---------------------------------------------------


async def test_a_successful_result_is_recorded_with_its_ids():
    session = FakeSession(
        outcome=CommandOutcome(
            "req-1",
            result=factories.make_result(
                request_id="req-1", object_id="offer-9", transaction_id="txn-4"
            ),
        )
    )
    executor, _, evidence, *_ = make_executor(session)

    await executor.execute(AcceptAction("offer-9"), "req-1")

    record = evidence.records[0]
    assert (record.result_ok, record.result_code) == (True, "OK")
    assert (record.object_id, record.transaction_id) == ("offer-9", "txn-4")


async def test_the_record_captures_the_state_the_decision_was_made_from():
    executor, _, evidence, *_ = make_executor()

    await executor.execute(OFFER, "req-1")

    record = evidence.records[0]
    assert record.observed_snapshot_sequence == 3
    assert record.observed_tick == 2


async def test_a_rejection_is_recorded_as_a_failure_not_as_sent():
    session = FakeSession(
        outcome=CommandOutcome(
            "req-1",
            result=factories.make_result(
                request_id="req-1", ok=False, code=ResultCode.INSUFFICIENT_RESOURCES
            ),
        )
    )
    executor, _, evidence, *_ = make_executor(session)

    outcome = await executor.execute(OFFER, "req-1")

    assert not outcome.ok
    assert evidence.records[0].result_ok is False
    assert evidence.records[0].result_code == "INSUFFICIENT_RESOURCES"


async def test_a_protocol_error_answer_is_recorded_distinctly():
    session = FakeSession(
        outcome=CommandOutcome(
            "req-1",
            error=factories.make_protocol_error(code=ControlCode.REQUEST_CAPACITY_EXCEEDED),
        )
    )
    executor, _, evidence, *_ = make_executor(session)

    await executor.execute(AdvertiseAction(frozenset(), frozenset(), 6), "req-1")

    record = evidence.records[0]
    assert record.result_ok is False
    assert record.result_code == "REQUEST_CAPACITY_EXCEEDED"
    assert any("protocol_error" in note for note in record.notes)


async def test_station_failed_removes_the_recipient_from_future_targeting():
    """Permanent failure removes trading eligibility for the rest of the run."""
    session = FakeSession(
        outcome=CommandOutcome(
            "req-1",
            result=factories.make_result(
                request_id="req-1", ok=False, code=ResultCode.STATION_FAILED
            ),
        )
    )
    executor, _, _, _, counterparties = make_executor(session)
    counterparties.update(factories.make_snapshot())

    await executor.execute(OFFER, "req-1")

    assert counterparties.get("P02").station_failed


# --- sync -----------------------------------------------------------------


async def test_sync_sends_no_request_id_and_records_evidence():
    executor, session, evidence, *_ = make_executor()

    await executor.sync(step="10")
    executor.confirm(factories.make_snapshot(snapshot_sequence=9))

    assert session.sent == [("sync", None, None)]
    record = evidence.records[0]
    assert record.action_kind == "sync"
    assert record.confirmed_snapshot_sequence == 9


def test_confirming_with_nothing_pending_is_harmless():
    executor, *_ = make_executor()

    executor.confirm(factories.make_snapshot())


# --- evidence file --------------------------------------------------------


async def test_evidence_is_appended_as_jsonl_and_contains_no_credentials(tmp_path):
    path = tmp_path / "nested" / "evidence.jsonl"
    session = FakeSession()
    evidence = EvidenceLog(path)
    executor = Executor(session, evidence)

    await executor.execute(OFFER, "req-1", step="4")
    executor.confirm(factories.make_snapshot(snapshot_sequence=4, world_version=5))

    rows = evidence.read_back()
    assert len(rows) == 1
    assert rows[0]["confirmed_world_version"] == 5
    assert rows[0]["action"]["give"] == {"water": 2, "food": 0, "components": 0}
    assert "token" not in path.read_text().lower()
