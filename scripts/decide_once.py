#!/usr/bin/env python3
"""Ask a strategy what it would do in one hand-written situation. No server needed.

    python scripts/decide_once.py scenarios/incoming-gift.json
    python scripts/decide_once.py scenarios/unfair-offer.json --strategy passive

A scenario file describes our planet and what is on the market; an optional
"expect" block states what a correct decision contains, and the exit code says
whether it did (tests/unit/test_scenarios.py runs every file in scenarios/):

    {
      "tick": 10, "specialty": "WATER",
      "inventory": {"water": 40, "food": 2, "components": 30},
      "incoming_offers": [{"offer_id": "offer-9", "from": "P02",
                           "they_give": {"food": 5}, "they_want": {"water": 5}}],
      "advertisements": [{"station": "P02", "selling": ["FOOD"], "seeking": ["WATER"]}],
      "expect": {"accepts": ["offer-9"], "pays_only_with": "WATER"}
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bazaar_client.domain.types import (  # noqa: E402
    Advertisement,
    Bundle,
    DirectoryEntry,
    Offer,
    OfferStatus,
    Phase,
    PublicationStatus,
    Resource,
    Snapshot,
    StationObservation,
)
from bazaar_client.execution.actions import AcceptAction, OfferAction  # noqa: E402
from bazaar_client.policy.memory import PolicyMemory  # noqa: E402
from bazaar_client.strategy import DEFAULT_STRATEGY, STRATEGIES, get_strategy  # noqa: E402
from bazaar_sim.economy import DEFAULT_RULES  # noqa: E402


def _bundle(values: dict | None) -> Bundle:
    return Bundle(**(values or {}))


def load_scenario(document: dict) -> Snapshot:
    me = document.get("station", "P01")
    tick = document.get("tick", 0)
    peers = sorted({o["from"] for o in document.get("incoming_offers", [])}
                   | {a["station"] for a in document.get("advertisements", [])} | {"P02"})
    offers = tuple(
        Offer(offer_id=o["offer_id"], proposer_id=o["from"], recipient_id=me,
              give=_bundle(o.get("they_give")), receive=_bundle(o.get("they_want")),
              created_tick=tick, created_version=1, expires_tick=o.get("expires_tick", tick + 5),
              status=OfferStatus.OPEN, closed_tick=None, transaction_id=None)
        for o in document.get("incoming_offers", [])
    )
    ads = tuple(
        Advertisement(advertisement_id=f"ad-{a['station']}", station_id=a["station"],
                      selling=frozenset(Resource[r] for r in a.get("selling", [])),
                      seeking=frozenset(Resource[r] for r in a.get("seeking", [])),
                      created_tick=tick, expires_tick=a.get("expires_tick", tick + 10),
                      created_version=1, status=PublicationStatus.ACTIVE)
        for a in document.get("advertisements", [])
    )
    zero = Bundle.zero()
    station = StationObservation(
        station_id=me, inventory=_bundle(document["inventory"]),
        health=document.get("health", 100), failed_once=False, first_failure_tick=None,
        last_production=zero, last_unmet_upkeep=zero, fully_supplied_ticks=0, shortage_ticks=0,
        current_shortage_streak=0, longest_shortage_streak=0, produced_total=zero,
        consumed_total=zero, unmet_total=zero, imported_total=zero, exported_total=zero,
        upkeep_per_tick=_bundle(document.get("upkeep", {"water": 1, "food": 1, "components": 1})),
        specialty=Resource[document.get("specialty", "WATER")],
    )
    return Snapshot(
        run_id="scenario", snapshot_sequence=1, world_version=1, tick=tick,
        phase=Phase[document.get("phase", "RUNNING")], self_station_id=me, rules=DEFAULT_RULES,
        directory=tuple(DirectoryEntry(s, s) for s in [me, *peers]), me=station,
        offers=offers, advertisements=ads, transactions=(), request_results=(), outcome=None,
    )


def check_expectations(decision, passes: dict[str, str], expect: dict) -> list[str]:
    """Each unmet expectation, as a sentence. Empty means the decision is as expected."""
    problems = []
    accepted = [a.offer_id for a in decision.actions if isinstance(a, AcceptAction)]
    offers = [a for a in decision.actions if isinstance(a, OfferAction)]
    if "accepts" in expect and sorted(accepted) != sorted(expect["accepts"]):
        problems.append(f"expected to accept {expect['accepts']}, accepted {accepted}")
    for offer_id, reason in expect.get("passes", {}).items():
        if reason not in passes.get(offer_id, ""):
            problems.append(f"expected {offer_id} passed because '{reason}', got {passes.get(offer_id)!r}")
    if "offers_to" in expect and sorted({o.recipient_id for o in offers}) != sorted(expect["offers_to"]):
        problems.append(f"expected offers to {expect['offers_to']}, made {[o.recipient_id for o in offers]}")
    if "pays_only_with" in expect:
        allowed = Resource[expect["pays_only_with"]]
        for o in offers:
            if any(o.give.get(r) for r in Resource if r is not allowed):
                problems.append(f"offer to {o.recipient_id} pays with more than {allowed.name}")
    if expect.get("no_actions") and decision.actions:
        problems.append(f"expected no actions, got {[a.kind for a in decision.actions]}")
    return problems


def evaluate(document: dict, strategy_name: str = DEFAULT_STRATEGY):
    snapshot = load_scenario(document)
    strategy = get_strategy(strategy_name)
    decision, _ = strategy.decide(snapshot, PolicyMemory(), command_budget=document.get("command_budget"))
    passes = strategy.explain_passes(snapshot, decision)
    return decision, passes, check_expectations(decision, passes, document.get("expect", {}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--strategy", choices=sorted(STRATEGIES), default=DEFAULT_STRATEGY)
    args = parser.parse_args(argv)
    document = json.loads(args.scenario.read_text(encoding="utf-8"))
    decision, passes, problems = evaluate(document, args.strategy)

    print(document.get("description", args.scenario.name))
    print(f"strategy {args.strategy}: {len(decision.actions)} action(s)")
    for action in decision.actions:
        print(f"  {action.kind:9} {json.dumps(action.describe())}")
    for reason in decision.reasons:
        print(f"  why: {reason}")
    for offer_id, reason in passes.items():
        print(f"  passed {offer_id}: {reason}")
    if "expect" in document:
        print("as expected" if not problems else "NOT as expected:\n  " + "\n  ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
