"""The run-log analyzer, on a tiny hand-built Directorate log."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "analyze_run.py"
spec = importlib.util.spec_from_file_location("analyze_run", SCRIPT)
analyze_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze_run)


def bundle(water=0, food=0, components=0):
    return {"water": water, "food": food, "components": components}


def station_totals(display_id, produced, inventory, failed_at, health):
    return {
        "display_id": display_id,
        "produced_total": produced,
        "first_failure_tick": failed_at,
        "health": health,
        "inventory": inventory,
        "imported_total": bundle(food=4),
        "exported_total": bundle(water=4),
        "unmet_total": bundle(food=2),
        "shortage_ticks": 2,
    }


def offer(give, receive, status):
    return {"proposer_display_id": "d1", "give": give, "receive": receive, "status": status}


LOG = {
    "run_id": "run-test",
    "snapshot": {
        "stations": [
            {"display_id": "d1", "station_id": "P01", "label": "Nova"},
            {"display_id": "d2", "station_id": "P02", "label": "Nacre"},
        ],
        "economic": {
            "totals": {
                "produced": bundle(10, 10, 0), "consumed": bundle(4, 4, 4),
                "ending": bundle(6, 6, 0),
            },
            "stations": [
                station_totals("d1", bundle(water=10), bundle(water=6), 7, 0),
                station_totals("d2", bundle(food=10), bundle(food=6), None, 100),
            ],
            "offers": [
                offer(bundle(water=2), bundle(food=4), "EXPIRED"),
                offer(bundle(water=3), bundle(food=3), "ACCEPTED"),
                offer(bundle(water=3), bundle(food=3), "ACCEPTED"),
                offer(bundle(water=1), bundle(), "ACCEPTED"),
            ],
        },
    },
    "history": [
        {"tick": 0, "stations": [
            {"display_id": "d1", "health": 100, "inventory": bundle(5, 5, 5)}]},
        {"tick": 0, "stations": [
            {"display_id": "d1", "health": 99, "inventory": bundle(1, 1, 1)}]},
        {"tick": 5, "stations": [
            {"display_id": "d1", "health": 50, "inventory": bundle(4, 0, 0)}]},
        {"tick": 10, "stations": [
            {"display_id": "d1", "health": 0, "inventory": bundle(6, 0, 0)}]},
    ],
}


def test_outcomes_name_each_planet_its_specialty_and_its_failure():
    rows = analyze_run.outcomes(LOG)

    assert [(r["station_id"], r["specialty"], r["first_failure_tick"]) for r in rows] == [
        ("P01", "water", 7),
        ("P02", "food", None),
    ]


def test_offers_are_classified_by_terms_and_outcome():
    breakdown = analyze_run.offer_breakdown(LOG, "P01")

    assert breakdown == {
        ("asks more than it gives", "EXPIRED"): 1,
        ("1:1", "ACCEPTED"): 2,
        ("gift", "ACCEPTED"): 1,
    }


def test_the_timeline_takes_the_first_sample_of_each_interval():
    assert analyze_run.timeline(LOG, "P01", every=10) == [
        (0, 100, bundle(5, 5, 5)),
        (10, 0, bundle(6, 0, 0)),
    ]


def test_the_report_runs_end_to_end_from_a_file(tmp_path, capsys):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(LOG))

    assert analyze_run.main([str(path), "--station", "P01", "--every", "5"]) == 0

    out = capsys.readouterr().out
    assert "1 of 2 planets failed" in out
    assert "left unused 6,6,0" in out
    assert "tick    5  health  50" in out
