"""The evidence-log analyzer, on a tiny hand-built client log."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "analyze_evidence.py"
spec = importlib.util.spec_from_file_location("analyze_evidence", SCRIPT)
analyze_evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze_evidence)


def bundle(water=0, food=0, components=0):
    return {"water": water, "food": food, "components": components}


def decision(tick, health, inventory, actions=(), reasons=("waiting",), ts=None):
    return {
        "kind": "decision", "timestamp": ts, "tick": tick, "health": health,
        "inventory": inventory, "available": inventory, "import_targets": bundle(food=10),
        "specialty_spendable": 5, "open_outgoing_offers": 0,
        "actions": [{"kind": k} for k in actions], "reasons": list(reasons),
    }


def action(kind, tick, ok=True, code="OK"):
    record = {"step": f"tick-{tick}", "action_kind": kind, "action": {},
              "request_id": f"r-{kind}-{tick}", "observed_tick": tick, "notes": []}
    if ok is not None:
        record.update(result_ok=ok, result_code=code)
    return record


def conn(event, ts, detail=None):
    record = {"kind": "connection", "timestamp": f"2026-09-28T10:00:{ts:02d}+00:00", "event": event}
    if detail:
        record["detail"] = detail
    return record


RECORDS = [
    conn("connecting", 0),
    conn("connected", 1),
    decision(0, 100, bundle(5, 5, 5)),
    decision(0, 100, bundle(5, 5, 5)),  # an idle repeat, as older clients logged on reconnect
    decision(1, 100, bundle(5, 3, 5), actions=["accept"], reasons=["accept: food we need"]),
    action("accept", 1),
    decision(2, 96, bundle(4, 0, 5), actions=["offer"], reasons=["offer: water for food"]),
    action("offer", 2, ok=False, code="INSUFFICIENT_INVENTORY"),
    conn("disconnected", 11, "connection reset"),
    conn("reconnecting", 12, "delay=1.0s"),
    conn("connecting", 13),
    conn("connected", 21),
    decision(3, 92, bundle(4, 0, 4), actions=["accept"]),
    action("accept", 3, ok=None),  # sent, then the connection dropped
    conn("closed", 31, "run FINISHED"),
]


def test_idle_repeats_of_a_tick_are_dropped():
    ticks = [d["tick"] for d in analyze_evidence.decisions(RECORDS)]

    assert ticks == [0, 1, 2, 3]


def test_commands_are_counted_by_kind_and_failures_by_code():
    assert analyze_evidence.action_summary(RECORDS) == {"accept": 2, "offer": 1}
    assert analyze_evidence.rejection_summary(RECORDS) == {
        "INSUFFICIENT_INVENTORY": 1,
        "NO_ANSWER": 1,
    }
    assert len(analyze_evidence.settled_trades(RECORDS)) == 1


def test_connection_events_become_up_and_down_periods():
    periods = analyze_evidence.connection_periods(RECORDS)

    assert [(p["state"], p["seconds"]) for p in periods] == [
        ("down", 1.0),   # first connect
        ("up", 10.0),
        ("down", 10.0),  # dropped at :11, back at :21
        ("up", 10.0),
    ]
    assert periods[2]["detail"] == "connection reset"
    assert analyze_evidence.total_downtime(periods) == 11.0


def test_a_log_without_connection_events_has_no_periods():
    assert analyze_evidence.connection_periods([decision(0, 100, bundle())]) == []


def test_shortages_flag_empty_stock_and_lost_health():
    rows = analyze_evidence.shortages(RECORDS)

    assert rows == [
        {"tick": 2, "health": 96, "health_lost": 4, "empty": ["food"]},
        {"tick": 3, "health": 92, "health_lost": 4, "empty": ["food"]},
    ]


def test_each_decision_carries_the_outcomes_it_caused():
    traces = analyze_evidence.traces(RECORDS)

    assert [(t["decision"]["tick"], [a["action_kind"] for a in t["outcomes"]]) for t in traces] == [
        (0, []), (1, ["accept"]), (2, ["offer"]), (3, ["accept"]),
    ]


SCRIPTED = [
    conn("connected", 0),
    action("advertise", 0),
    action("advertise", 0, ok=False, code="REQUEST_CAPACITY_EXCEEDED"),
    {**action("sync", 0, ok=None), "notes": ["control message; answered by a state only"]},
    conn("closed", 1, "walkthrough: 63 of 63 checks passed"),
]


def test_commands_without_a_policy_decision_still_get_rows():
    """The walkthrough sends commands without running the policy."""
    traces = analyze_evidence.traces(SCRIPTED)

    assert [t["decision"] for t in traces] == [None, None, None]
    assert [t["outcomes"][0]["action_kind"] for t in traces] == ["advertise", "advertise", "sync"]


def test_sync_is_neither_ok_nor_rejected():
    assert analyze_evidence.rejection_summary(SCRIPTED) == {"REQUEST_CAPACITY_EXCEEDED": 1}
    overview = analyze_evidence.overview(SCRIPTED)
    assert (overview["commands_sent"], overview["commands_ok"], overview["commands_rejected"]) == (3, 1, 1)


def test_a_scripted_run_reports_no_decisions_rather_than_none_values():
    out = analyze_evidence.report(SCRIPTED)

    assert "no policy decisions logged" in out
    assert "None" not in out


def test_ticks_with_no_decision_are_reported_as_gaps():
    records = [decision(t, 100, bundle(5, 5, 5)) for t in (0, 1, 2, 10, 11)]

    assert analyze_evidence.decision_gaps(records) == [(3, 9)]
    assert "no decisions logged for ticks 3..9" in analyze_evidence.report(records)


def test_the_text_report_summarises_the_run():
    out = analyze_evidence.report(RECORDS, every=2)

    assert "commands: 3 sent, 1 ok, 2 rejected; 1 trades settled" in out
    assert "total downtime 11s" in out
    assert "first at tick 2" in out
    assert "tick    2  health  96  stock 4,0,5" in out


def test_the_html_report_is_self_contained():
    page = analyze_evidence.html_report(RECORDS, "Test <run>")

    assert "<title>Test &lt;run&gt;</title>" in page
    assert "INSUFFICIENT_INVENTORY" in page
    assert "http://" not in page and "https://" not in page


def test_embedded_data_cannot_close_the_script_tag():
    hostile = [decision(0, 100, bundle(), reasons=["</script><b>x"])]

    page = analyze_evidence.html_report(hostile)

    assert "</script><b>" not in page


def test_the_report_runs_end_to_end_from_a_file(tmp_path, capsys):
    path = tmp_path / "evidence.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n")
    html_path = tmp_path / "out" / "report.html"

    assert analyze_evidence.main([str(path), "--html", str(html_path)]) == 0

    assert "4 decisions logged" in capsys.readouterr().out
    assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")
