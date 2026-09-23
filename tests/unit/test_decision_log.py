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
