#!/usr/bin/env python3
"""Every check, in one command, with a record of exactly what was tested.

    python scripts/check.py                  # everything that runs without a practice server
    python scripts/check.py --integration    # also the practice-server tests (in the container / CI)
    python scripts/check.py --quick          # tests only: skip the live simulation and benchmark

Steps, in order; the run stops at nothing and reports every result:

  bindings     bazaar_pb2.py exists and imports
  compile      every source file compiles
  tests        unit, wire, state, survival, simulator and server tests, with coverage
  integration  the real practice server's scripted exchange (--integration)
  simulation   our server plus three client processes over real sockets
  benchmark    a small benchmark, then --check proves it reproduces exactly

The build id (branch@commit, and whether the tree had uncommitted changes) is
printed first and last and saved with every step's outcome in
logs/check-report.json, so "which revision passed?" always has an answer.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bazaar_client.version import build_info, describe_build  # noqa: E402

COVERAGE_FLOOR = 85
PY = sys.executable


def steps(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    out = REPO / "logs" / "check"
    plan = [
        ("bindings", [PY, "-c", "import bazaar_pb2, websockets; print('bindings import')"]),
        ("compile", [PY, "-m", "compileall", "-q", "bazaar_client", "bazaar_sim", "scripts", "tests"]),
        ("tests", [PY, "-m", "pytest", "-q", "-m", "not integration", "-p", "no:cacheprovider",
                   "--cov=bazaar_client", "--cov=bazaar_sim", "--cov-branch",
                   "--cov-report=term", f"--cov-fail-under={COVERAGE_FLOOR}"]),
    ]
    if args.integration or shutil.which("bazaar-server"):
        plan.append(("integration", [PY, "-m", "pytest", "-q", "-m", "integration",
                                     "-p", "no:cacheprovider"]))
    if not args.quick:
        plan += [
            ("simulation", [PY, "-m", "bazaar_sim.orchestrate", "--planets", "3", "--ticks", "10",
                            "--tick-ms", "150", "--out", str(out / "sim"), "--no-dashboards"]),
            ("benchmark", [PY, "-m", "bazaar_sim.benchmark", "--seeds", "1", "--slots", "0",
                           "--planets", "3", "--out", str(out / "benchmark.json"),
                           "--markdown", str(out / "benchmark.md")]),
            ("reproduce", [PY, "-m", "bazaar_sim.benchmark", "--check", str(out / "benchmark.json")]),
        ]
    return plan


def run(name: str, command: list[str]) -> dict:
    print(f"\n=== {name}: {' '.join(Path(c).name if i == 0 else c for i, c in enumerate(command))}",
          flush=True)
    started = time.perf_counter()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")]))}
    completed = subprocess.run(command, cwd=REPO, env=env)
    seconds = round(time.perf_counter() - started, 1)
    verdict = "passed" if completed.returncode == 0 else f"FAILED (exit {completed.returncode})"
    print(f"=== {name}: {verdict} in {seconds}s", flush=True)
    return {"step": name, "command": command[1:], "returncode": completed.returncode, "seconds": seconds}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--integration", action="store_true",
                        help="also run the practice-server tests (needs bazaar-server on PATH)")
    parser.add_argument("--quick", action="store_true", help="skip the simulation and benchmark")
    parser.add_argument("--report", type=Path, default=REPO / "logs" / "check-report.json")
    args = parser.parse_args(argv)

    info = build_info()
    print(f"checking {describe_build(info)} on Python {info['python']}", flush=True)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = [run(name, command) for name, command in steps(args)]
    passed = all(r["returncode"] == 0 for r in results)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        **info, "started": started, "passed": passed, "steps": results,
        "ci_commit": os.environ.get("GITHUB_SHA"),
    }, indent=2), encoding="utf-8")

    print("\nsummary")
    for r in results:
        print(f"  {'ok ' if r['returncode'] == 0 else 'FAIL'} {r['step']:12} {r['seconds']:>6}s")
    print(f"{'ALL CHECKS PASSED' if passed else 'CHECKS FAILED'} for {describe_build(info)}; "
          f"record in {args.report.relative_to(REPO) if args.report.is_relative_to(REPO) else args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
