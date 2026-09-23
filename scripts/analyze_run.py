#!/usr/bin/env python3
"""Summarise a Directorate run log (for example run-2-log.json).

Prints how every planet ended, how the galaxy's resources were used, and, for
one station, how its offers fared and how its health and stock moved over time.

    python scripts/analyze_run.py run-2-log.json --station P01

Standard library only, so any team can run it without this project installed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

RESOURCES = ("water", "food", "components")


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _labels(log: dict) -> dict[str, tuple[str, str]]:
    """display_id -> (station_id, label)."""
    return {
        s["display_id"]: (s["station_id"], s.get("label", ""))
        for s in log["snapshot"]["stations"]
    }


def _display_id(log: dict, station_id: str) -> str:
    for display_id, (sid, _) in _labels(log).items():
        if sid == station_id:
            return display_id
    raise KeyError(f"no station {station_id} in this log")


def _fmt(bundle: dict) -> str:
    return ",".join(str(bundle.get(r, 0)) for r in RESOURCES)


def outcomes(log: dict) -> list[dict]:
    """One row per planet: specialty, failure tick, and final flows."""
    labels = _labels(log)
    rows = []
    for station in log["snapshot"]["economic"]["stations"]:
        station_id, label = labels.get(station["display_id"], ("?", ""))
        produced = station["produced_total"]
        rows.append({
            "station_id": station_id,
            "label": label,
            "specialty": max(produced, key=produced.get),
            "first_failure_tick": station["first_failure_tick"],
            "health": station["health"],
            "inventory": station["inventory"],
            "produced": produced,
            "imported": station["imported_total"],
            "exported": station["exported_total"],
            "unmet": station["unmet_total"],
            "shortage_ticks": station["shortage_ticks"],
        })
    return sorted(rows, key=lambda r: r["station_id"])


def _terms(offer: dict) -> str:
    give, receive = sum(offer["give"].values()), sum(offer["receive"].values())
    if receive == 0:
        return "gift"
    if give == receive:
        return "1:1"
    return "asks more than it gives" if receive > give else "gives more than it asks"


def offer_breakdown(log: dict, station_id: str) -> Counter:
    """(terms, status) counts for every offer the station proposed."""
    me = _display_id(log, station_id)
    return Counter(
        (_terms(o), o["status"])
        for o in log["snapshot"]["economic"]["offers"]
        if o["proposer_display_id"] == me
    )


def timeline(log: dict, station_id: str, every: int = 10) -> list[tuple[int, int, dict]]:
    """(tick, health, inventory) at every `every` ticks, first sample of each tick."""
    me = _display_id(log, station_id)
    seen: set[int] = set()
    points = []
    for entry in log["history"]:
        tick = entry["tick"]
        if tick % every or tick in seen:
            continue
        seen.add(tick)
        for station in entry["stations"]:
            if station["display_id"] == me:
                points.append((tick, station["health"], station["inventory"]))
    return points


def report(log: dict, station_id: str | None = None, every: int = 10) -> str:
    lines = []
    totals = log["snapshot"]["economic"]["totals"]
    rows = outcomes(log)
    failed = sum(1 for r in rows if r["first_failure_tick"] is not None)
    lines.append(f"run {log.get('run_id', '?')}: {failed} of {len(rows)} planets failed")
    lines.append(
        f"galaxy (water,food,components): produced {_fmt(totals['produced'])}  "
        f"consumed {_fmt(totals['consumed'])}  left unused {_fmt(totals['ending'])}"
    )
    lines.append("")
    lines.append(
        f"{'id':4} {'label':9} {'specialty':11} {'failed':>6} {'hp':>4} "
        f"{'inventory':>13} {'imported':>11} {'exported':>11} {'unmet':>9}"
    )
    for r in rows:
        failed_at = "-" if r["first_failure_tick"] is None else str(r["first_failure_tick"])
        lines.append(
            f"{r['station_id']:4} {r['label']:9} {r['specialty']:11} {failed_at:>6} "
            f"{r['health']:>4} {_fmt(r['inventory']):>13} {_fmt(r['imported']):>11} "
            f"{_fmt(r['exported']):>11} {_fmt(r['unmet']):>9}"
        )

    if station_id:
        breakdown = offer_breakdown(log, station_id)
        lines.append("")
        lines.append(f"{station_id} offers: {sum(breakdown.values())} proposed")
        for (terms, status), count in sorted(breakdown.items()):
            lines.append(f"  {terms:24} {status:10} {count:>4}")
        lines.append("")
        lines.append(f"{station_id} over time (water,food,components):")
        for tick, health, inventory in timeline(log, station_id, every):
            lines.append(f"  tick {tick:>4}  health {health:>3}  stock {_fmt(inventory)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path, help="Directorate run log JSON")
    parser.add_argument("--station", help="station id to examine in detail, e.g. P01")
    parser.add_argument("--every", type=int, default=10, help="timeline interval in ticks")
    args = parser.parse_args(argv)
    print(report(load(args.log), args.station, args.every))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
