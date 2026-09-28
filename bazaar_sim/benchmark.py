"""Compare strategies against baselines on matched scenarios, seeds and seats.

    python -m bazaar_sim.benchmark                                  # default matrix
    python -m bazaar_sim.benchmark --seeds 10 --out logs/bench.json --markdown logs/bench.md
    python -m bazaar_sim.benchmark --check logs/bench.json           # reproduce every row

Every candidate plays exactly the same cases -- scenario, seed, seat and
opponents -- so a difference in score is the strategy's doing. A seed fixes the
production phase, each planet's starting stock and the opponents' seating.
Every run is reported, failed ones included, and the output records the code
revision, interpreter and command line so any row can be re-run and checked.

Success is defined in `bazaar_sim.world` before any comparison is made:
collective survival first, then survivors, world alive ticks and shortage
ticks, and only then the planet under test.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from bazaar_client.strategy import STRATEGIES, get_strategy
from bazaar_client.version import build_info, describe_build
from bazaar_sim import world
from bazaar_sim.opponents import (
    Greedy,
    Quitter,
    SmallFair,
    StrategyPlayer,
    lineup,
    run_two_opponents,
)
from bazaar_sim.world import (
    RUN_TICKS,
    balanced_production,
    run_two_production,
    run_world,
    seeded_conditions,
)

FIELD_PLANETS = 9

FIELDS: dict[str, Callable[[], list]] = {
    "run2-field": run_two_opponents,
    "fair-field": lambda: [SmallFair() for _ in range(FIELD_PLANETS - 1)],
    "quitters": lambda: [Quitter() for _ in range(FIELD_PLANETS - 1)],
}
SELF_PLAY = "self-play-balanced"
SCENARIOS = (*FIELDS, SELF_PLAY)

CANDIDATES: dict[str, Callable[[], object]] = {
    **{name: (lambda name=name: StrategyPlayer(get_strategy(name))) for name in STRATEGIES},
    "small-fair": SmallFair,
    "greedy": Greedy,
}
DEFAULT_CANDIDATES = ("reserve-trader", "passive", "small-fair")
BASELINES = ("passive", "small-fair")


@dataclass(frozen=True)
class Case:
    scenario: str
    seed: int
    slot: int
    planets: int
    candidate: str
    ticks: int = RUN_TICKS


def build_cases(
    scenarios, seeds, slots, candidates, self_play_planets, ticks: int = RUN_TICKS
) -> list[Case]:
    cases = []
    for scenario in scenarios:
        for seed in seeds:
            if scenario == SELF_PLAY:
                for planets in self_play_planets:
                    cases += [Case(scenario, seed, 0, planets, c, ticks) for c in candidates]
            else:
                for slot in slots:
                    cases += [Case(scenario, seed, slot, FIELD_PLANETS, c, ticks) for c in candidates]
    return cases


def run_case(case: Case) -> dict:
    """Play one case and score it. Deterministic: the same case gives the same row."""
    phase_offset, stock = seeded_conditions(case.seed, case.planets)
    if case.scenario == SELF_PLAY:
        # Several copies of one strategy, output exactly covering upkeep.
        players = [CANDIDATES[case.candidate]() for _ in range(case.planets)]
        production = balanced_production(case.planets, variation=1)
    else:
        opponents = FIELDS[case.scenario]()
        random.Random(case.seed).shuffle(opponents)
        players = lineup(CANDIDATES[case.candidate](), opponents, case.slot)
        production = run_two_production(phase_offset)
    started = time.perf_counter()
    outcome = run_world(players, case.ticks, production=production, starting_stock=stock)
    subject = f"P{case.slot + 1:02}"
    score = outcome.score(subject)
    return {
        **asdict(case),
        "subject": subject,
        "score": score.as_dict(),
        "failed": {sid: tick for sid, tick in sorted(outcome.failed_at.items())},
        "transactions": len(outcome.economy.transactions),
        "seconds": round(time.perf_counter() - started, 3),
    }


def _matched_key(row: dict) -> tuple:
    return (row["scenario"], row["seed"], row["slot"], row["planets"])


def _rank(row: dict) -> tuple:
    return world.Score(**row["score"]).rank_key()


def summarize(rows: list[dict]) -> dict:
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["scenario"], row["candidate"]), []).append(row)
    summary = []
    for (scenario, candidate), group in sorted(groups.items()):
        scores = [r["score"] for r in group]
        summary.append({
            "scenario": scenario,
            "candidate": candidate,
            "runs": len(group),
            "collective_successes": sum(s["collective_success"] for s in scores),
            "subject_survived": sum(bool(s["subject_survived"]) for s in scores),
            "failed_runs": sum(not s["collective_success"] for s in scores),
            "mean_survivors": round(statistics.mean(s["survivors"] for s in scores), 2),
            "mean_world_alive_ticks": round(statistics.mean(s["world_alive_ticks"] for s in scores), 1),
            "min_world_alive_ticks": min(s["world_alive_ticks"] for s in scores),
            "mean_shortage_ticks": round(statistics.mean(s["shortage_ticks"] for s in scores), 1),
        })

    by_case = {}
    for row in rows:
        by_case.setdefault(_matched_key(row), {})[row["candidate"]] = row
    comparisons = []
    candidates = sorted({r["candidate"] for r in rows})
    for candidate in candidates:
        for baseline in BASELINES:
            if baseline == candidate or baseline not in candidates:
                continue
            wins = ties = losses = 0
            for matched in by_case.values():
                if candidate in matched and baseline in matched:
                    ours, theirs = _rank(matched[candidate]), _rank(matched[baseline])
                    wins += ours > theirs
                    ties += ours == theirs
                    losses += ours < theirs
            comparisons.append({"candidate": candidate, "baseline": baseline,
                                "wins": wins, "ties": ties, "losses": losses})
    return {"by_scenario": summary, "versus_baselines": comparisons}


def run_benchmark(cases: list[Case]) -> dict:
    started = datetime.now(timezone.utc)
    rows = [run_case(case) for case in cases]
    return {
        "meta": {
            **build_info(),
            "started": started.isoformat(timespec="seconds"),
            "cases": len(cases),
            "success_definition": world.SUCCESS_DEFINITION,
        },
        "summary": summarize(rows),
        "rows": rows,
    }


def check(path: Path) -> tuple[int, list[str]]:
    """Re-run every recorded case; any difference is a reproducibility failure."""
    recorded = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    for row in recorded["rows"]:
        case = Case(**{k: row[k] for k in Case.__dataclass_fields__})
        again = run_case(case)
        for field in ("score", "failed", "transactions"):
            if again[field] != row[field]:
                problems.append(f"{case}: {field} was {row[field]}, now {again[field]}")
    meta = recorded["meta"]
    if meta.get("build") != build_info()["build"]:
        problems.insert(0, f"note: recorded on {meta.get('build')}, checking on {build_info()['build']}")
    return len(recorded["rows"]), problems


def markdown(result: dict) -> str:
    meta = result["meta"]
    dirty = {True: "uncommitted changes", False: "clean", None: "unknown"}[meta["dirty"]]
    lines = [
        "# Strategy benchmark",
        "",
        f"Build `{meta['build']}` ({dirty}), Python {meta['python']}, {meta['started']}, "
        f"{meta['cases']} runs. Command: `python -m bazaar_sim.benchmark {' '.join(meta['argv'])}`",
        "",
        "## Success, defined before comparing",
        "",
        "```",
        meta["success_definition"],
        "```",
        "",
        "## Results by scenario",
        "",
        "| scenario | candidate | runs | all survive | subject survives | failed runs | "
        "mean survivors | mean world ticks | min world ticks | mean shortage ticks |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in result["summary"]["by_scenario"]:
        lines.append(
            f"| {s['scenario']} | {s['candidate']} | {s['runs']} | {s['collective_successes']} | "
            f"{s['subject_survived']} | {s['failed_runs']} | {s['mean_survivors']} | "
            f"{s['mean_world_alive_ticks']} | {s['min_world_alive_ticks']} | {s['mean_shortage_ticks']} |"
        )
    lines += ["", "## Head to head on matched cases", "",
              "| candidate | vs baseline | wins | ties | losses |", "|---|---|---|---|---|"]
    for c in result["summary"]["versus_baselines"]:
        lines.append(f"| {c['candidate']} | {c['baseline']} | {c['wins']} | {c['ties']} | {c['losses']} |")
    failed = [r for r in result["rows"] if not r["score"]["collective_success"]]
    lines += ["", f"## Failed runs ({len(failed)} of {len(result['rows'])})", ""]
    if not failed:
        lines.append("None: every planet survived every run.")
    else:
        lines += ["| scenario | seed | slot | planets | candidate | failed planets (tick) |",
                  "|---|---|---|---|---|---|"]
        for r in failed:
            planets = ", ".join(f"{sid} ({tick})" for sid, tick in r["failed"].items())
            lines.append(f"| {r['scenario']} | {r['seed']} | {r['slot']} | {r['planets']} | "
                         f"{r['candidate']} | {planets} |")
    lines += ["", "Reproduce: `python -m bazaar_sim.benchmark --check <this run's JSON>`", ""]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bazaar-benchmark", description=__doc__.splitlines()[0])
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--candidates", nargs="+", choices=sorted(CANDIDATES),
                        default=list(DEFAULT_CANDIDATES))
    parser.add_argument("--seeds", type=int, default=5, help="seeds 1..N")
    parser.add_argument("--slots", type=int, nargs="+", default=[0, 1, 2],
                        help="seats for the candidate in 9-planet fields (0-8)")
    parser.add_argument("--planets", type=int, nargs="+", default=[3, 5, 9],
                        help="world sizes for the balanced self-play scenario")
    parser.add_argument("--ticks", type=int, default=RUN_TICKS)
    parser.add_argument("--out", type=Path, default=Path("logs/benchmark.json"))
    parser.add_argument("--markdown", type=Path, default=Path("logs/benchmark.md"))
    parser.add_argument("--check", type=Path, help="re-run a saved benchmark and compare")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        count, problems = check(args.check)
        for problem in problems:
            print(problem)
        mismatches = [p for p in problems if not p.startswith("note:")]
        print(f"reproduced {count - len(mismatches)} of {count} runs exactly")
        return 1 if mismatches else 0

    if any(not 0 <= s < FIELD_PLANETS for s in args.slots):
        print(f"--slots must be between 0 and {FIELD_PLANETS - 1}", file=sys.stderr)
        return 2
    cases = build_cases(args.scenarios, range(1, args.seeds + 1), args.slots,
                        args.candidates, args.planets, args.ticks)
    print(f"benchmarking {len(cases)} runs on {describe_build()}", flush=True)
    result = run_benchmark(cases)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    report = markdown(result)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(report, encoding="utf-8")
    print(report)
    print(f"wrote {args.out} and {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
