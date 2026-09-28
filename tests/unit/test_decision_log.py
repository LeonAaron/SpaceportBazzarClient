"""The per-tick decision log and the build identifier.

Run 2 could only be diagnosed from the Directorate's own log. These records let
us see, from our side, what the policy saw and why it acted.
"""

from __future__ import annotations

import json

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.execution.evidence import EvidenceLog
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.version import build_id
from tests.fixtures import factories


def decided_snapshot():
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(water=40, food=2, components=30)),
        advertisements=(
            factories.make_advertisement(
                station_id="P02", selling=frozenset({Resource.FOOD}), expires_tick=99
            ),
        ),
    )
    decision, _ = decide(snapshot, PolicyMemory())
    return snapshot, decision


def test_a_decision_record_explains_what_was_seen_and_chosen(tmp_path):
    path = tmp_path / "evidence.jsonl"
    log = EvidenceLog(path)
    snapshot, decision = decided_snapshot()

    log.decision(snapshot, decision)

    record = json.loads(path.read_text().splitlines()[0])
    assert record["kind"] == "decision"
    assert record["tick"] == snapshot.tick
    assert record["inventory"] == {"water": 40, "food": 2, "components": 30}
    assert record["import_targets"]["water"] == 0
    assert record["import_targets"]["food"] > 0
    assert record["specialty_spendable"] == decision.spendable
    assert [a["kind"] for a in record["actions"]] == [a.kind for a in decision.actions]
    assert record["reasons"] == decision.reasons


def test_decision_records_are_kept_in_memory_without_a_file():
    log = EvidenceLog()
    snapshot, decision = decided_snapshot()

    log.decision(snapshot, decision)

    assert log.decisions[0]["kind"] == "decision"


def test_every_record_carries_a_wall_clock_timestamp(tmp_path):
    """Ticks stop while we are disconnected; only real time shows how long for."""
    from datetime import datetime

    from bazaar_client.execution.actions import AcceptAction

    log = EvidenceLog(tmp_path / "evidence.jsonl")
    snapshot, decision = decided_snapshot()

    log.decision(snapshot, decision)
    log.complete(log.start("tick-0", AcceptAction(offer_id="offer-1"), snapshot))
    log.connection_event("connected")

    for record in log.read_back():
        assert datetime.fromisoformat(record["timestamp"]).tzinfo is not None


def test_connection_events_are_structured_records(tmp_path):
    log = EvidenceLog(tmp_path / "evidence.jsonl")

    log.connection_event("disconnected", detail="connection reset")
    log.connection_event("connected")

    first, second = log.read_back()
    assert first["kind"] == "connection"
    assert (first["event"], first["detail"]) == ("disconnected", "connection reset")
    assert second["event"] == "connected" and "detail" not in second


def test_a_restart_keeps_the_previous_runs_evidence(tmp_path):
    """A crash then a restart must not wipe the record of what led to the crash."""
    path = tmp_path / "evidence.jsonl"
    EvidenceLog(path).connection_event("disconnected", detail="crashed here")

    log = EvidenceLog(path)

    assert log.rotated_to is not None and log.rotated_to.exists()
    assert "crashed here" in log.rotated_to.read_text()
    assert path.read_text() == ""
    EvidenceLog(path)  # an empty file is not worth keeping
    assert len(list(tmp_path.glob("evidence.*.jsonl"))) == 1


def test_a_run_is_framed_by_start_and_end_records(tmp_path):
    log = EvidenceLog(tmp_path / "evidence.jsonl")
    log.run_start(build="main@abc", strategy="reserve-trader")
    log.status_event("participating", previous="synchronized", detail="tick 0")
    log.run_end(decisions=3)

    start, status, end = log.read_back()
    assert (start["kind"], start["build"], start["strategy"]) == ("run_start", "main@abc", "reserve-trader")
    assert (status["previous"], status["status"]) == ("synchronized", "participating")
    assert (end["kind"], end["decisions"]) == ("run_end", 3)


def test_decisions_record_open_offers_new_trades_and_passes_from_our_side(tmp_path):
    from bazaar_client.domain.types import Bundle

    outgoing = factories.make_offer(offer_id="o-out", proposer_id="P01", recipient_id="P02",
                                    give=Bundle(water=4), receive=Bundle(food=4), expires_tick=9)
    incoming = factories.make_offer(offer_id="o-in", proposer_id="P02", recipient_id="P01",
                                    give=Bundle(food=1), receive=Bundle(water=3), expires_tick=9)
    trade = factories.make_transaction(transaction_id="t-1", proposer_id="P02", recipient_id="P01",
                                       give=Bundle(food=2), receive=Bundle(water=2))
    snapshot = factories.make_snapshot(offers=(outgoing, incoming), transactions=(trade,))
    decision, _ = decide(snapshot, PolicyMemory())
    log = EvidenceLog(tmp_path / "evidence.jsonl")

    first = log.decision(snapshot, decision, passes={"o-in": "asks more than it gives"},
                         context={"session": 2, "timing": {"decide_ms": 1.5}})
    log.decision(snapshot, decision)

    one, two = log.read_back()
    assert (first, one["decision_id"], two["decision_id"]) == ("d1", "d1", "d2")
    assert one["open_offers"] == [
        {"offer_id": "o-out", "direction": "outgoing", "counterparty": "P02",
         "we_pay": {"water": 4, "food": 0, "components": 0},
         "we_get": {"water": 0, "food": 4, "components": 0}, "expires_tick": 9},
        {"offer_id": "o-in", "direction": "incoming", "counterparty": "P02",
         "we_pay": {"water": 3, "food": 0, "components": 0},
         "we_get": {"water": 0, "food": 1, "components": 0}, "expires_tick": 9},
    ]
    assert one["new_transactions"][0]["we_got"] == {"water": 0, "food": 2, "components": 0}
    assert two["new_transactions"] == []  # a trade is reported once
    assert one["passed_offers"] == {"o-in": "asks more than it gives"}
    assert (one["session"], one["timing"]) == (2, {"decide_ms": 1.5})
    assert one["run_id"] == snapshot.run_id and one["phase"] == "RUNNING"


def fake_git(tmp_path, head, refs=None, packed=None):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(head)
    for ref, sha in (refs or {}).items():
        (git / ref).parent.mkdir(parents=True, exist_ok=True)
        (git / ref).write_text(sha + "\n")
    if packed is not None:
        (git / "packed-refs").write_text(packed)
    return tmp_path


SHA = "65468f7bc8a94ea545b4ecc2d95d34b7d897aae8"


def test_the_build_names_the_branch_and_commit(tmp_path):
    root = fake_git(tmp_path, "ref: refs/heads/main\n", refs={"refs/heads/main": SHA})

    assert build_id(root) == "main@65468f7bc8a9"


def test_a_packed_branch_is_still_found(tmp_path):
    root = fake_git(tmp_path, "ref: refs/heads/main\n", packed=f"{SHA} refs/heads/main\n")

    assert build_id(root) == "main@65468f7bc8a9"


def test_a_detached_checkout_reports_its_commit(tmp_path):
    root = fake_git(tmp_path, SHA + "\n")

    assert build_id(root) == "65468f7bc8a9"


def test_outside_a_checkout_the_build_is_unknown(tmp_path):
    assert build_id(tmp_path) == "unknown"
