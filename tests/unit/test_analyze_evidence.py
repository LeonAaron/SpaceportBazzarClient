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


def test_shortage_streaks_groups_consecutive_ticks():
    rows = [
        {"tick": 2, "health": 96, "health_lost": 4, "empty": ["food"]},
        {"tick": 3, "health": 92, "health_lost": 4, "empty": ["food"]},
        {"tick": 7, "health": 80, "health_lost": 6, "empty": ["water"]},
    ]

    streaks = analyze_evidence.shortage_streaks(rows)

    assert streaks == [
        {"start": 2, "end": 3, "length": 2, "health_lost": 8, "empty": ["food"]},
        {"start": 7, "end": 7, "length": 1, "health_lost": 6, "empty": ["water"]},
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


def observed(tick, decision_id, *, actions=(), open_offers=(), trades=(), passed=None, wait=None,
             timing=None, skipped=None, reasons=("waiting",)):
    record = decision(tick, 100, bundle(10, 10, 10), actions=actions, reasons=reasons)
    record.update(decision_id=decision_id, open_offers=list(open_offers),
                  new_transactions=list(trades), timing=timing or {"queue_ms": 1.0, "decide_ms": 2.0})
    if passed:
        record["passed_offers"] = passed
    if wait:
        record["wait_reason"] = wait
    if skipped:
        record["ticks_skipped"] = skipped
    return record


INCOMING = {"offer_id": "offer-9", "direction": "incoming", "counterparty": "P02",
            "we_pay": bundle(water=2), "we_get": bundle(food=2), "expires_tick": 8}
TRADE = {"transaction_id": "txn-4", "offer_id": "offer-9", "counterparty": "P02",
         "we_paid": bundle(water=2), "we_got": bundle(food=2), "settled_tick": 4}

NEW_STYLE = [
    {"kind": "run_start", "timestamp": "2026-09-28T10:00:00+00:00", "build": "main@abc",
     "dirty": False, "strategy": "reserve-trader", "python": "3.12.1"},
    observed(3, "d1", open_offers=[INCOMING], passed={"offer-9": "worth accepting, but budget"},
             actions=["advertise"]),
    {**action("advertise", 3), "decision_id": "d1", "response_ms": 4.0, "confirm_ms": 6.0,
     "processed_tick": 3, "deadline_missed": False},
    observed(4, "d2", open_offers=[INCOMING], actions=["accept"],
             reasons=("accept offer-9: our specialty for what we import",)),
    {**action("accept", 4), "decision_id": "d2", "action": {"offer_id": "offer-9"},
     "transaction_id": "txn-4", "inventory_after": bundle(8, 12, 10), "response_ms": 8.0,
     "confirm_ms": 2.0, "processed_tick": 5, "deadline_missed": True},
    observed(5, "d3", trades=[TRADE], wait="nothing met the strategy's criteria to act"),
    observed(9, "d4", wait="no command slot is available this tick", skipped=3),
    {"kind": "run_end", "timestamp": "2026-09-28T10:01:00+00:00",
     "status_seconds": {"participating": 58.0, "stale": 2.0}},
]


def test_the_story_of_an_incoming_offer_runs_from_knowledge_to_settlement():
    story = analyze_evidence.offer_story(NEW_STYLE, "offer-9")

    assert [line.split()[0] for line in story] == [
        "knew", "passed", "decided", "sent", "server", "settled"]
    assert "P02 offers us 0,2,0 for 2,0,0" in story[0]
    assert "worth accepting, but budget" in story[1]
    assert "our specialty for what we import" in story[2]
    assert "txn-4" in story[4] and "8,12,10" in story[4]


def test_an_unknown_offer_says_so():
    assert analyze_evidence.offer_story(NEW_STYLE, "offer-404") == ["offer-404 does not appear in this log"]


def test_responsiveness_is_measured_per_stage_with_deadlines():
    speed = analyze_evidence.responsiveness(NEW_STYLE)

    assert set(speed["series"]) == {"queue", "decide", "response", "confirm"}
    assert speed["series"]["response"] == {"count": 2, "p50": 4.0, "p95": 8.0, "max": 8.0}
    assert (speed["missed_deadlines"], speed["deadline_checks"]) == (1, 2)


def test_participation_separates_acting_waiting_and_stalling():
    active = analyze_evidence.participation(NEW_STYLE)

    assert (active["acted"], active["waited"]) == (2, 2)
    assert active["wait_reasons"] == {"nothing met the strategy's criteria to act": 1,
                                      "no command slot is available this tick": 1}
    assert active["skipped_ticks"] == [(9, 3)]
    assert active["status_seconds"] == {"participating": 58.0, "stale": 2.0}


def test_every_completed_trade_is_listed_once_whoever_accepted():
    assert analyze_evidence.completed_trades(NEW_STYLE + [observed(10, "d5", trades=[TRADE])]) == [TRADE]


def test_trade_balance_sums_net_resources_moved():
    other = {"transaction_id": "txn-5", "offer_id": "offer-1", "counterparty": "P03",
              "we_paid": bundle(food=3), "we_got": bundle(water=1, components=2), "settled_tick": 6}

    assert analyze_evidence.trade_balance([TRADE, other]) == {"water": -1, "food": -1, "components": 2}


def test_key_findings_ranks_by_severity_and_covers_expected_ids():
    findings = analyze_evidence.key_findings(RECORDS)

    ids = [f["id"] for f in findings]
    assert set(ids) == {"dominant_rejection", "shortage_streak", "health_drop"}
    severities = [f["severity"] for f in findings]
    assert severities == sorted(severities, key=lambda s: {"bad": 0, "warn": 1, "info": 2}[s])
    by_id = {f["id"]: f for f in findings}
    assert by_id["shortage_streak"] == {"id": "shortage_streak", "severity": "bad",
                                         "start": 2, "end": 3, "length": 2, "health_lost": 8,
                                         "empty": ["food"]}


def test_key_findings_is_empty_for_a_clean_run():
    clean = [decision(0, 100, bundle(5, 5, 5)), decision(1, 100, bundle(5, 5, 5))]

    assert analyze_evidence.key_findings(clean) == []


def test_report_includes_key_findings_section():
    out = analyze_evidence.report(RECORDS)

    assert "key findings:" in out
    assert "INSUFFICIENT_INVENTORY" in out


def test_the_new_sections_appear_in_the_text_and_the_page():
    text = analyze_evidence.report(NEW_STYLE)
    page = analyze_evidence.html_report(NEW_STYLE)

    assert text.startswith("build main@abc, clean; strategy reserve-trader")
    assert "1 trades completed" in text
    assert "missed deadlines: 1 of 2" in text
    assert "stalled? 3 tick(s) passed with no decision before tick 9" in text
    assert "Responsiveness" in page and "Completed trades" in page and "txn-4" in page
    assert "Key findings" in page


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


def test_write_reports_returns_empty_for_a_missing_or_empty_log(tmp_path):
    missing = tmp_path / "missing-evidence.jsonl"
    empty = tmp_path / "empty-evidence.jsonl"
    empty.write_text("")

    assert analyze_evidence.write_reports(missing) == {}
    assert analyze_evidence.write_reports(empty) == {}


def test_write_reports_writes_a_summary_and_a_dashboard(tmp_path):
    path = tmp_path / "P01-evidence.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n")

    written = analyze_evidence.write_reports(path)

    assert written == {"summary": tmp_path / "P01-summary.txt", "dashboard": tmp_path / "P01-dashboard.html"}
    assert "key findings:" in written["summary"].read_text(encoding="utf-8")
    assert written["dashboard"].read_text(encoding="utf-8").startswith("<!doctype html>")


def test_the_report_runs_end_to_end_from_a_file(tmp_path, capsys):
    path = tmp_path / "evidence.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n")
    html_path = tmp_path / "out" / "report.html"

    assert analyze_evidence.main([str(path), "--html", str(html_path)]) == 0

    assert "4 decisions logged" in capsys.readouterr().out
    assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")
