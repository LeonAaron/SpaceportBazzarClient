#!/usr/bin/env python3
"""Summarise our own client's evidence log (the --evidence-file JSONL).

Where analyze_run.py reads the Directorate's log, this reads what *we* recorded:
what the policy saw each tick, what it decided and why, what it sent, what the
server answered, and when the connection was up or down.

    python scripts/analyze_evidence.py logs/live-evidence.jsonl
    python scripts/analyze_evidence.py logs/live-evidence.jsonl --html logs/report.html
    python scripts/analyze_evidence.py logs/live-evidence.jsonl --offer offer-37

Record kinds (one JSON object per line; see bazaar_client/execution/evidence.py):
    run_start   build, strategy and configuration of the process
    status      starting / connecting / ... / participating / stale / finished
    connection  connecting / connected / disconnected / reconnecting / closed / failed
    decision    decision_id, tick, what was seen (health, inventory, open offers,
                new trades), the verdict (act or wait) with reasons, and why each
                incoming offer was passed over; timing of the wait and the decision
    (command)   action_kind, decision_id, request_id, result, object/transaction id,
                response and confirmation times, whether it missed its tick
    run_end     final counts, latency percentiles, seconds spent in each status

Older logs lacking the newer fields are summarised with what they have.
Standard library only, so any team can run it without this project installed.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

RESOURCES = ("water", "food", "components")
DOWN_EVENTS = ("disconnected", "reconnecting", "closed", "failed")
LATENCY_SERIES = ("queue", "decide", "response", "confirm")


def load(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def is_action(record: dict) -> bool:
    return "action_kind" in record


def is_command(record: dict) -> bool:
    """An action that expects a result -- unlike sync, which by protocol design never gets one."""
    return is_action(record) and record["action_kind"] != "sync"


def is_connection(record: dict) -> bool:
    return record.get("kind") == "connection"


def decisions(records: list[dict]) -> list[dict]:
    """Decision records, dropping repeats of an idle tick (older logs re-logged them on reconnect)."""
    seen: set[int] = set()
    out = []
    for r in records:
        if r.get("kind") != "decision":
            continue
        if not r.get("actions") and r["tick"] in seen:
            continue
        seen.add(r["tick"])
        out.append(r)
    return out


def action_summary(records: list[dict]) -> Counter:
    return Counter(r["action_kind"] for r in records if is_action(r))


def rejection_summary(records: list[dict]) -> Counter:
    return Counter(
        r.get("result_code") or "NO_ANSWER"
        for r in records
        if is_command(r) and r.get("result_ok") is not True
    )


def settled_trades(records: list[dict]) -> list[dict]:
    """Accepts the server confirmed: the only commands that actually move goods."""
    return [
        r for r in records
        if is_action(r) and r["action_kind"] == "accept" and r.get("result_ok") is True
    ]


def resource_timeline(records: list[dict], every: int = 10) -> list[tuple[int, int, dict]]:
    """(tick, health, inventory) at every `every` ticks, first sample of each tick."""
    points, seen = [], set()
    for d in decisions(records):
        tick = d["tick"]
        if tick % every or tick in seen:
            continue
        seen.add(tick)
        points.append((tick, d["health"], d["inventory"]))
    return points


def shortages(records: list[dict]) -> list[dict]:
    """Ticks where a resource ran out or health fell since the previous tick."""
    rows, last_health = [], None
    for d in decisions(records):
        empty = [r for r in RESOURCES if d["inventory"].get(r, 0) <= 0]
        drop = (last_health - d["health"]) if last_health is not None else 0
        if empty or drop > 0:
            rows.append({"tick": d["tick"], "health": d["health"], "health_lost": max(drop, 0),
                         "empty": empty})
        last_health = d["health"]
    return rows


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def connection_periods(records: list[dict]) -> list[dict]:
    """Alternating up/down intervals built from connection events.

    Down time starts at the first connect attempt or at a drop, and ends at the
    next successful connect; up time ends at a drop or a clean close.
    """
    stamped = [r for r in records if r.get("timestamp")]
    if not stamped:
        return []
    last_ts = max(_ts(r["timestamp"]) for r in stamped)

    periods: list[dict] = []
    up_since = down_since = None
    down_detail = None

    def close(state, start, end, detail=None):
        periods.append({
            "state": state, "start": start.isoformat(), "end": end.isoformat(),
            "seconds": (end - start).total_seconds(), "detail": detail,
        })

    for r in records:
        if not is_connection(r):
            continue
        ts, event = _ts(r["timestamp"]), r["event"]
        if event == "connected":
            if down_since is not None:
                close("down", down_since, ts, down_detail)
                down_since = None
            up_since = up_since or ts
        elif event in DOWN_EVENTS:
            if up_since is not None:
                close("up", up_since, ts)
                up_since = None
            if event != "closed" and down_since is None:
                down_since, down_detail = ts, r.get("detail")
            if event == "closed":
                down_since = None
        elif event == "connecting" and up_since is None and down_since is None:
            down_since, down_detail = ts, "connecting"

    if up_since is not None:
        close("up", up_since, last_ts)
    if down_since is not None:
        close("down", down_since, last_ts, down_detail)
    return periods


def total_downtime(periods: list[dict]) -> float:
    return sum(p["seconds"] for p in periods if p["state"] == "down")


def traces(records: list[dict]) -> list[dict]:
    """Stimuli -> decision -> outcome, one entry per logged decision.

    Action records are written right after the decision that produced them, so
    each attaches to the most recent decision in file order.

    Commands with no decision before them (the scripted walkthrough sends
    commands without running the policy) get an entry of their own with
    `decision` set to None.
    """
    out: list[dict] = []
    current = None
    keep = {id(d) for d in decisions(records)}
    for r in records:
        if r.get("kind") == "decision":
            if id(r) in keep:
                current = {"decision": r, "outcomes": []}
                out.append(current)
        elif is_action(r):
            if current is None:
                out.append({"decision": None, "outcomes": [r]})
            else:
                current["outcomes"].append(r)
    return out


def decision_gaps(records: list[dict], min_ticks: int = 3) -> list[tuple[int, int]]:
    """Tick ranges with no decision logged: the client was down or stalled."""
    ticks = sorted({d["tick"] for d in decisions(records)})
    return [(a + 1, b - 1) for a, b in zip(ticks, ticks[1:]) if b - a > min_ticks]


def run_header(records: list[dict]) -> dict:
    """The process's build, strategy and configuration, if it recorded them."""
    return next((r for r in records if r.get("kind") == "run_start"), {})


def completed_trades(records: list[dict]) -> list[dict]:
    """Every trade we were party to, whoever accepted, as first seen in a decision record."""
    seen, trades = set(), []
    for d in records:
        for t in d.get("new_transactions", ()) if d.get("kind") == "decision" else ():
            if t["transaction_id"] not in seen:
                seen.add(t["transaction_id"])
                trades.append(t)
    return trades


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest rank: always a value that was actually observed."""
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def responsiveness(records: list[dict]) -> dict:
    """Per-stage latency percentiles, sample counts and missed deadlines."""
    samples: dict[str, list[float]] = {name: [] for name in LATENCY_SERIES}
    for r in records:
        timing = r.get("timing") if r.get("kind") == "decision" else None
        if timing:
            for key in ("queue", "decide"):
                if timing.get(f"{key}_ms") is not None:
                    samples[key].append(timing[f"{key}_ms"])
        if is_command(r):
            for key in ("response", "confirm"):
                if r.get(f"{key}_ms") is not None:
                    samples[key].append(r[f"{key}_ms"])
    series = {}
    for name, values in samples.items():
        if values:
            ordered = sorted(values)
            series[name] = {"count": len(ordered), "p50": _percentile(ordered, 0.5),
                            "p95": _percentile(ordered, 0.95), "max": ordered[-1]}
    checked = [r for r in records if is_command(r) and "deadline_missed" in r]
    return {
        "series": series,
        "deadline_checks": len(checked),
        "missed_deadlines": sum(1 for r in checked if r["deadline_missed"]),
        "decided_on_superseded_state": sum(
            1 for d in decisions(records) if d.get("states_during_decision", 0) > 0),
    }


def participation(records: list[dict]) -> dict:
    """Acting versus deliberately waiting, and evidence of a stalled client."""
    ds = decisions(records)
    waits = Counter(
        d.get("wait_reason") or (d.get("reasons") or ["no action"])[0]
        for d in ds if not d.get("actions")
    )
    stalls = [(d["tick"], d["ticks_skipped"]) for d in ds if d.get("ticks_skipped")]
    end = next((r for r in reversed(records) if r.get("kind") == "run_end"), {})
    return {
        "acted": sum(1 for d in ds if d.get("actions")),
        "waited": sum(waits.values()),
        "wait_reasons": dict(waits.most_common()),
        "skipped_ticks": stalls,
        "stale_events": [r for r in records if r.get("kind") == "status" and r["status"] == "stale"],
        "status_seconds": end.get("status_seconds", {}),
        "statuses": [
            {"timestamp": r["timestamp"], "status": r["status"], "detail": r.get("detail", "")}
            for r in records if r.get("kind") == "status"
        ],
    }


def offer_story(records: list[dict], offer_id: str) -> list[str]:
    """Everything the log knows about one offer: what we knew, decided, sent and got back."""
    lines = []
    first_seen = next((d for d in decisions(records)
                       if any(o["offer_id"] == offer_id for o in d.get("open_offers", ()))), None)
    ours = next((r for r in records if is_command(r) and r.get("object_id") == offer_id
                 and r["action_kind"] == "offer"), None)
    if ours is not None:
        made_by = next((d for d in records if d.get("kind") == "decision"
                        and d.get("decision_id") == ours.get("decision_id")), None)
        if made_by is not None:
            lines.append(f"knew    tick {made_by['tick']}: health {made_by['health']}, stock "
                         f"{_fmt(made_by['inventory'])}, targets {_fmt(made_by.get('import_targets'))}, "
                         f"spendable {made_by.get('specialty_spendable')}")
            lines.append(f"decided {made_by['decision_id']}: " + "; ".join(made_by.get("reasons", [])))
        lines.append(f"sent    {ours['request_id']}: {json.dumps(ours['action'])}")
        lines.append(f"server  {ours.get('result_code')} at tick {ours.get('processed_tick')} "
                     f"in {ours.get('response_ms')}ms, confirmed by state "
                     f"{ours.get('confirmed_snapshot_sequence')}")
    elif first_seen is not None:
        view = next(o for o in first_seen["open_offers"] if o["offer_id"] == offer_id)
        lines.append(f"knew    tick {first_seen['tick']}: {view['counterparty']} offers us "
                     f"{_fmt(view['we_get'])} for {_fmt(view['we_pay'])} (water,food,components), "
                     f"expires t{view['expires_tick']}; our stock {_fmt(first_seen['inventory'])}")
    for d in decisions(records):
        passed = d.get("passed_offers", {}).get(offer_id)
        if passed:
            lines.append(f"passed  tick {d['tick']} ({d['decision_id']}): {passed}")
            break
    accept = next((r for r in records if is_command(r) and r["action_kind"] == "accept"
                   and r.get("action", {}).get("offer_id") == offer_id), None)
    if accept is not None:
        made_by = next((d for d in records if d.get("kind") == "decision"
                        and d.get("decision_id") == accept.get("decision_id")), None)
        why = next((reason for reason in (made_by or {}).get("reasons", []) if offer_id in reason), "")
        lines.append(f"decided {accept.get('decision_id')} at tick {accept.get('observed_tick')}: {why}")
        lines.append(f"sent    {accept['request_id']}: accept")
        lines.append(f"server  {accept.get('result_code')}, transaction {accept.get('transaction_id')}, "
                     f"stock afterwards {_fmt(accept.get('inventory_after'))}")
    trade = next((t for t in completed_trades(records) if t["offer_id"] == offer_id), None)
    if trade is not None:
        lines.append(f"settled {trade['transaction_id']} at tick {trade['settled_tick']} with "
                     f"{trade['counterparty']}: paid {_fmt(trade['we_paid'])}, got {_fmt(trade['we_got'])}")
    elif lines:
        lines.append("settled no: never became a trade we were party to in this log")
    return lines or [f"{offer_id} does not appear in this log"]


def _fmt(bundle: dict | None) -> str:
    bundle = bundle or {}
    return ",".join(str(bundle.get(r, 0)) for r in RESOURCES)


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def overview(records: list[dict]) -> dict:
    ds = decisions(records)
    actions = [r for r in records if is_action(r)]
    commands = [r for r in records if is_command(r)]
    periods = connection_periods(records)
    short = shortages(records)
    last = ds[-1] if ds else None
    records_trades = any("new_transactions" in d for d in ds)
    header = run_header(records)
    return {
        "build": header.get("build"),
        "strategy": header.get("strategy"),
        "trades_completed": len(completed_trades(records)) if records_trades else None,
        "decisions": len(ds),
        "acting_decisions": sum(1 for d in ds if d.get("actions")),
        "first_tick": ds[0]["tick"] if ds else None,
        "last_tick": last["tick"] if last else None,
        "final_health": last["health"] if last else None,
        "final_inventory": last["inventory"] if last else None,
        "commands_sent": len(actions),
        "commands_ok": sum(1 for a in commands if a.get("result_ok") is True),
        "commands_rejected": sum(1 for a in commands if a.get("result_ok") is not True),
        "trades_settled": len(settled_trades(records)),
        "connections": sum(1 for r in records if is_connection(r) and r["event"] == "connected"),
        "downtime_s": total_downtime(periods),
        "first_shortage_tick": short[0]["tick"] if short else None,
        "health_lost": sum(s["health_lost"] for s in short),
    }


def report(records: list[dict], every: int = 10) -> str:
    o = overview(records)
    lines = []
    header = run_header(records)
    if header:
        dirty = {True: ", uncommitted changes", False: ", clean", None: ""}[header.get("dirty")]
        lines.append(f"build {header.get('build')}{dirty}; strategy {header.get('strategy')}; "
                     f"python {header.get('python')}")
    if o["decisions"]:
        lines += [
            f"ticks {o['first_tick']}..{o['last_tick']}: {o['decisions']} decisions logged, "
            f"{o['acting_decisions']} acted",
            f"final health {o['final_health']}  stock (water,food,components) "
            f"{_fmt(o['final_inventory'])}",
        ]
        for start, end in decision_gaps(records):
            lines.append(f"no decisions logged for ticks {start}..{end} (down or stalled)")
    else:
        lines.append("no policy decisions logged (a scripted run records its commands only)")
    trades = (f"{o['trades_completed']} trades completed"
              if o["trades_completed"] is not None
              else f"{o['trades_settled']} trades settled by our accepts")
    lines += [
        f"commands: {o['commands_sent']} sent, {o['commands_ok']} ok, "
        f"{o['commands_rejected']} rejected; {trades}",
        "",
        "commands by kind:",
    ]
    for kind, count in sorted(action_summary(records).items()):
        lines.append(f"  {kind:12} {count:>4}")
    rejections = rejection_summary(records)
    lines.append("rejections by code:" if rejections else "rejections by code: none")
    for code, count in rejections.most_common():
        lines.append(f"  {code:24} {count:>4}")

    lines.append("")
    periods = connection_periods(records)
    if periods:
        lines.append(
            f"connection: {o['connections']} connects, total downtime "
            f"{_duration(o['downtime_s'])}"
        )
        for p in periods:
            if p["state"] == "down" and p["seconds"] >= 0.5:
                lines.append(
                    f"  down {p['start']} for {_duration(p['seconds'])}"
                    + (f" ({p['detail']})" if p["detail"] else "")
                )
    else:
        lines.append("connection: no connection events in this log (recorded by newer clients)")

    lines.append("")
    short = shortages(records)
    if short:
        lines.append(
            f"shortages: first at tick {o['first_shortage_tick']}, {len(short)} ticks affected, "
            f"{o['health_lost']} health lost"
        )
        for s in short[:10]:
            empty = f" out of {','.join(s['empty'])}" if s["empty"] else ""
            lines.append(f"  tick {s['tick']:>4}  health {s['health']:>3} (-{s['health_lost']}){empty}")
        if len(short) > 10:
            lines.append(f"  ... {len(short) - 10} more")
    else:
        lines.append("shortages: none")

    speed = responsiveness(records)
    if speed["series"]:
        lines += ["", "responsiveness (ms):"]
        for name, s in speed["series"].items():
            lines.append(f"  {name:9} n={s['count']:<5} p50={s['p50']:<8} p95={s['p95']:<8} max={s['max']}")
        if speed["deadline_checks"]:
            lines.append(f"  missed deadlines: {speed['missed_deadlines']} of {speed['deadline_checks']} "
                         "commands processed in a later tick than decided")
        lines.append(f"  decisions made while a newer state was arriving: "
                     f"{speed['decided_on_superseded_state']}")

    active = participation(records)
    if active["acted"] or active["waited"]:
        lines += ["", f"participation: acted on {active['acted']} decisions, "
                      f"waited on {active['waited']} by choice"]
        for reason, count in active["wait_reasons"].items():
            lines.append(f"  wait {count:>4}x  {reason}")
        for tick, skipped in active["skipped_ticks"]:
            lines.append(f"  stalled? {skipped} tick(s) passed with no decision before tick {tick}")
        if active["stale_events"]:
            lines.append(f"  state went stale {len(active['stale_events'])} time(s)")
        if active["status_seconds"]:
            spent = ", ".join(f"{k} {v:.1f}s" for k, v in active["status_seconds"].items() if v)
            lines.append(f"  time by status: {spent}")

    timeline = resource_timeline(records, every)
    if timeline:
        lines.append("")
        lines.append("over time (water,food,components):")
        for tick, health, inventory in timeline:
            lines.append(f"  tick {tick:>4}  health {health:>3}  stock {_fmt(inventory)}")
    return "\n".join(lines)


# --- HTML -------------------------------------------------------------------


def _outcome(a: dict) -> dict:
    sync = a["action_kind"] == "sync"
    return {
        "kind": a["action_kind"],
        "ok": True if sync else a.get("result_ok"),
        "code": "SENT" if sync else (a.get("result_code") or "NO_ANSWER"),
        "request_id": a.get("request_id"),
        "object_id": a.get("object_id"),
        "inventory_after": a.get("inventory_after"),
        "notes": a.get("notes", []),
    }


def _trace_row(t: dict) -> dict:
    d = t["decision"]
    outcomes = [_outcome(a) for a in t["outcomes"]]
    if d is None:
        a = t["outcomes"][0]
        return {
            "tick": a.get("observed_tick"), "timestamp": a.get("timestamp"), "health": None,
            "inventory": None, "targets": None, "spendable": None, "open_offers": None,
            "scripted": True, "actions": [{"kind": a["action_kind"], **a.get("action", {})}],
            "reasons": [f"scripted step {a.get('step', '?')} (no policy decision)"],
            "outcomes": outcomes,
        }
    return {
        "tick": d["tick"], "timestamp": d.get("timestamp"), "health": d["health"],
        "inventory": d["inventory"], "targets": d.get("import_targets"),
        "spendable": d.get("specialty_spendable"), "open_offers": d.get("open_outgoing_offers"),
        "scripted": False, "actions": d.get("actions", []), "reasons": d.get("reasons", []),
        "outcomes": outcomes, "decision_id": d.get("decision_id"),
        "wait_reason": d.get("wait_reason"), "passed": d.get("passed_offers", {}),
        "timing": d.get("timing"), "skipped": d.get("ticks_skipped"),
    }


def _page_data(records: list[dict]) -> dict:
    return {
        "overview": overview(records),
        "header": run_header(records),
        "speed": responsiveness(records),
        "participation": participation(records),
        "trades": completed_trades(records),
        "traces": [_trace_row(t) for t in traces(records)],
        "gaps": decision_gaps(records),
        "periods": connection_periods(records),
        "actions": dict(action_summary(records)),
        "rejections": dict(rejection_summary(records).most_common()),
        "shortages": shortages(records),
    }


def html_report(records: list[dict], title: str = "Run report") -> str:
    data = json.dumps(_page_data(records)).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__DATA__", data)


HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;
--water:#2f6fb3;--food:#3f8f4a;--components:#b0702a;--health:#b3303a;--ok:#3f8f4a;--bad:#b3303a;--down:#b3303a;--up:#3f8f4a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecece8;--muted:#9a9a93;--line:#34342f;
--water:#6ea6e6;--food:#79c485;--components:#e0a35c;--health:#ef6b73;--ok:#79c485;--bad:#ef6b73;--down:#ef6b73;--up:#79c485}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 20px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.kpi span{color:var(--muted);font-size:12px}
svg{display:block;width:100%;height:auto}svg text{fill:var(--muted);font-size:11px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:10px;height:3px;margin-right:5px;vertical-align:middle}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media (max-width:720px){.grid2{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}td.n{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:8px;border-radius:4px;background:var(--bad)}.bar.k{background:var(--water)}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.controls button,.controls input{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.controls button[aria-pressed=true]{background:var(--ink);color:var(--panel)}
.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:12px;border:1px solid var(--line);margin:1px 2px 1px 0}
.tag.ok{color:var(--ok);border-color:var(--ok)}.tag.bad{color:var(--bad);border-color:var(--bad)}
.reasons{margin:0;padding-left:16px;color:var(--muted)}.tracewrap{max-height:640px;overflow:auto}
tr.sel{outline:2px solid var(--water)}
#tip{position:fixed;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;display:none;max-width:320px;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.empty{color:var(--muted)}
</style></head><body><main>
<h1>__TITLE__</h1>
<p class="sub" id="sub">Generated from the client's evidence log: what the policy saw, what it decided and why, and what the server answered.</p>
<section class="panel"><div class="kpis" id="kpis"></div></section>
<section class="panel"><h2>Health and stock by tick</h2><svg id="chart" viewBox="0 0 1000 300"></svg>
<div class="legend"><span><i style="background:var(--health)"></i>health</span><span><i style="background:var(--water)"></i>water</span><span><i style="background:var(--food)"></i>food</span><span><i style="background:var(--components)"></i>components</span><span>click a tick to jump to its decision</span></div></section>
<section class="panel"><h2>Connection</h2><div id="conn"></div></section>
<div class="grid2">
<section class="panel"><h2>Commands by kind</h2><div id="kinds"></div></section>
<section class="panel"><h2>Rejected or unanswered commands</h2><div id="rejects"></div></section>
</div>
<div class="grid2">
<section class="panel"><h2>Responsiveness</h2><div id="speed"></div></section>
<section class="panel"><h2>Participation and status</h2><div id="active"></div></section>
</div>
<section class="panel"><h2>Completed trades</h2><div class="tracewrap" style="max-height:320px" id="trades"></div></section>
<section class="panel"><h2>Stimuli &rarr; decision &rarr; outcome</h2>
<div class="controls"><button data-f="all" aria-pressed="false">All ticks</button><button data-f="acted" aria-pressed="true">Acted</button><button data-f="rejected" aria-pressed="false">Rejected</button><button data-f="short" aria-pressed="false">Shortage</button><input id="q" placeholder="Search reasons, codes..." size="24"></div>
<div class="tracewrap"><table><thead><tr><th>Tick</th><th>Saw (stimuli)</th><th>Decided</th><th>Why</th><th>Server answered</th></tr></thead><tbody id="traces"></tbody></table></div></section>
</main><div id="tip"></div>
<script>
const D=__DATA__;const R=["water","food","components"];const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=b=>b?R.map(r=>b[r]??0).join(" / "):"-";
const dur=s=>{s=Math.round(s);const m=Math.floor(s/60);return m?m+"m"+String(s%60).padStart(2,"0")+"s":s+"s"};
const tip=$("#tip");function showTip(e,h){tip.innerHTML=h;tip.style.display="block";tip.style.left=Math.min(e.clientX+12,innerWidth-330)+"px";tip.style.top=(e.clientY+12)+"px"}function hideTip(){tip.style.display="none"}
const O=D.overview,HD=D.header,SP=D.speed,PA=D.participation;
if(HD.build)$("#sub").textContent+=` Build ${HD.build}${HD.dirty?" (uncommitted changes)":""}, strategy ${HD.strategy}.`;
const k=[["Ticks",O.first_tick==null?"-":O.first_tick+"–"+O.last_tick],["Final health",O.final_health??"-"],["Commands sent",O.commands_sent],["Rejected",O.commands_rejected],[O.trades_completed==null?"Trades settled":"Trades completed",O.trades_completed??O.trades_settled],["Downtime",D.periods.length?dur(O.downtime_s):"n/a"],["First shortage",O.first_shortage_tick??"none"],["Health lost",O.health_lost]];
$("#kpis").innerHTML=k.map(([l,v])=>`<div class="kpi"><b>${esc(v)}</b><span>${l}</span></div>`).join("");
(function speed(){const S=Object.entries(SP.series);const el=$("#speed");if(!S.length){el.innerHTML='<p class="empty">No timings in this log (recorded by newer clients).</p>';return}
const label={queue:"state waited before deciding",decide:"strategy computing",response:"command sent → answer",confirm:"answer → confirming state"};
el.innerHTML="<table><tr><th>stage</th><th class='n'>n</th><th class='n'>p50 ms</th><th class='n'>p95 ms</th><th class='n'>max ms</th></tr>"+S.map(([n,s])=>`<tr><td>${esc(n)}<div style="color:var(--muted);font-size:12px">${label[n]||""}</div></td><td class="n">${s.count}</td><td class="n">${s.p50}</td><td class="n">${s.p95}</td><td class="n">${s.max}</td></tr>`).join("")+"</table>"+
`<p style="margin:8px 0 0">Missed deadlines: <b>${SP.missed_deadlines}</b> of ${SP.deadline_checks} commands · decided while a newer state arrived: <b>${SP.decided_on_superseded_state}</b></p>`})();
(function active(){const el=$("#active");const W=Object.entries(PA.wait_reasons);let h=`<p style="margin:0 0 8px">Acted on <b>${PA.acted}</b> decisions, waited by choice on <b>${PA.waited}</b>.</p>`;
if(W.length)h+="<table>"+W.map(([r,n])=>`<tr><td>${esc(r)}</td><td class="n">${n}</td></tr>`).join("")+"</table>";
if(PA.skipped_ticks.length)h+=`<p class="tag bad" style="margin-top:8px">${PA.skipped_ticks.length} stall(s): ticks passed with no decision</p>`;
const secs=Object.entries(PA.status_seconds).filter(([,v])=>v>0);if(secs.length){const tot=secs.reduce((a,[,v])=>a+v,0);h+=`<div style="margin-top:10px">`+secs.map(([s,v])=>`<span class="tag ${s=="stale"||s=="disconnected"?"bad":s=="participating"?"ok":""}">${esc(s)} ${(100*v/tot).toFixed(0)}%</span>`).join("")+"</div>"}
if(PA.statuses.length)h+=`<details style="margin-top:8px"><summary>${PA.statuses.length} status changes</summary><table>`+PA.statuses.map(s=>`<tr><td>${esc(s.timestamp.slice(11,23))}</td><td>${esc(s.status)}</td><td style="color:var(--muted)">${esc(s.detail)}</td></tr>`).join("")+"</table></details>";
el.innerHTML=h})();
(function trades(){const T=D.trades,el=$("#trades");if(!T.length){el.innerHTML='<p class="empty">No completed trades recorded (older logs only record our own accepts).</p>';return}
el.innerHTML="<table><tr><th>tick</th><th>trade</th><th>with</th><th>we paid (w/f/c)</th><th>we got (w/f/c)</th></tr>"+T.map(t=>`<tr><td class="n">${t.settled_tick}</td><td>${esc(t.transaction_id)}<div style="color:var(--muted);font-size:12px">${esc(t.offer_id)}</div></td><td>${esc(t.counterparty)}</td><td>${fmt(t.we_paid)}</td><td>${fmt(t.we_got)}</td></tr>`).join("")+"</table>"})();
const shortTicks=new Set(D.shortages.map(s=>s.tick));
(function chart(){const T=D.traces.filter(t=>t.health!=null);const svg=$("#chart");if(!T.length){svg.outerHTML='<p class="empty">No policy decisions logged, so there is no stock history to plot. This run\\'s commands are listed in the table below.</p>';return}
const W=1000,H=300,L=44,Rt=44,Tp=12,B=28,t0=T[0].tick,t1=Math.max(T[T.length-1].tick,t0+1);
const maxInv=Math.max(1,...T.flatMap(t=>R.map(r=>t.inventory[r]||0)));const maxH=Math.max(100,...T.map(t=>t.health));
const x=t=>L+(t-t0)/(t1-t0)*(W-L-Rt),yi=v=>H-B-v/maxInv*(H-B-Tp),yh=v=>H-B-v/maxH*(H-B-Tp);
let s="";for(let i=0;i<=4;i++){const y=Tp+i*(H-B-Tp)/4;s+=`<line x1="${L}" x2="${W-Rt}" y1="${y}" y2="${y}" stroke="var(--line)"/><text x="${L-6}" y="${y+4}" text-anchor="end">${Math.round(maxInv*(1-i/4))}</text><text x="${W-Rt+6}" y="${y+4}">${Math.round(maxH*(1-i/4))}</text>`}
const step=Math.max(1,Math.ceil((t1-t0)/10));for(let t=t0;t<=t1;t+=step)s+=`<text x="${x(t)}" y="${H-8}" text-anchor="middle">${t}</text>`;
T.filter(t=>shortTicks.has(t.tick)).forEach(t=>s+=`<rect x="${x(t.tick)-1.5}" y="${Tp}" width="3" height="${H-B-Tp}" fill="var(--bad)" opacity=".15"/>`);
D.gaps.forEach(([a,b])=>{const g0=x(a-1),g1=x(b+1);const lbl=`no decisions logged, ticks ${a}–${b}`;s+=`<rect x="${g0}" y="${Tp}" width="${g1-g0}" height="${H-B-Tp}" fill="var(--muted)" opacity=".12"><title>${lbl}</title></rect>`+(g1-g0>190?`<text x="${(g0+g1)/2}" y="${Tp+16}" text-anchor="middle">${lbl}</text>`:"")});
const segs=[];let cur=[];T.forEach((t,i)=>{if(i&&t.tick-T[i-1].tick>3){segs.push(cur);cur=[]}cur.push(t)});segs.push(cur);
const line=(f,c,w)=>segs.map(sg=>`<polyline fill="none" stroke="${c}" stroke-width="${w}" points="${sg.map(t=>x(t.tick).toFixed(1)+","+f(t).toFixed(1)).join(" ")}"/>`).join("");
R.forEach(r=>s+=line(t=>yi(t.inventory[r]||0),`var(--${r})`,1.8));s+=line(t=>yh(t.health),"var(--health)",2.4);
s+=`<line id="cross" y1="${Tp}" y2="${H-B}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>`;
s+=`<rect id="hit" x="${L}" y="${Tp}" width="${W-L-Rt}" height="${H-B-Tp}" fill="transparent" style="cursor:crosshair"/>`;svg.innerHTML=s;
const near=e=>{const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;const q=p.matrixTransform(svg.getScreenCTM().inverse());let best=T[0];for(const t of T)if(Math.abs(x(t.tick)-q.x)<Math.abs(x(best.tick)-q.x))best=t;return best};
const hit=$("#hit"),cross=$("#cross");
hit.addEventListener("mousemove",e=>{const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;const at=t0+(p.matrixTransform(svg.getScreenCTM().inverse()).x-L)/(W-L-Rt)*(t1-t0);
const gap=D.gaps.find(([a,b])=>at>=a-0.5&&at<=b+0.5);if(gap){cross.setAttribute("visibility","hidden");showTip(e,`<b>ticks ${gap[0]}–${gap[1]}</b><br>no decisions logged: the client was disconnected or stalled`);return}
const t=near(e);cross.setAttribute("x1",x(t.tick));cross.setAttribute("x2",x(t.tick));cross.setAttribute("visibility","visible");
showTip(e,`<b>tick ${t.tick}</b> · health ${t.health}<br>stock ${fmt(t.inventory)}<br>${t.actions.length?"acted: "+esc(t.actions.map(a=>a.kind).join(", ")):"no action"}${t.reasons.length?"<br><span style='color:var(--muted)'>"+esc(t.reasons[0])+"</span>":""}`)});
hit.addEventListener("mouseleave",()=>{hideTip();cross.setAttribute("visibility","hidden")});
hit.addEventListener("click",e=>jump(near(e).tick))})();
(function conn(){const P=D.periods,el=$("#conn");if(!P.length){el.innerHTML='<p class="empty">No connection events in this log (recorded by newer clients).</p>';return}
const a=Date.parse(P[0].start),b=Math.max(a+1,Date.parse(P[P.length-1].end));
let s=`<svg viewBox="0 0 1000 40">`;P.forEach((p,i)=>{const x0=(Date.parse(p.start)-a)/(b-a)*1000,x1=(Date.parse(p.end)-a)/(b-a)*1000;s+=`<rect data-i="${i}" x="${x0}" y="6" width="${Math.max(2,x1-x0)}" height="22" fill="var(--${p.state})" opacity="${p.state=="up"?.55:.9}"/>`});
s+=`</svg><div class="legend"><span><i style="background:var(--up)"></i>connected</span><span><i style="background:var(--down)"></i>not connected</span><span>${P.filter(p=>p.state=="down").length} gaps, ${dur(O.downtime_s)} total</span></div>`;el.innerHTML=s;
el.querySelectorAll("rect").forEach(r=>{const p=P[+r.dataset.i];r.addEventListener("mousemove",e=>showTip(e,`<b>${p.state=="up"?"connected":"not connected"}</b> for ${dur(p.seconds)}<br>${esc(p.start)}${p.detail?"<br>"+esc(p.detail):""}`));r.addEventListener("mouseleave",hideTip)})})();
function bars(obj,el,cls){const e=Object.entries(obj);if(!e.length){el.innerHTML='<p class="empty">None.</p>';return}const m=Math.max(...e.map(([,v])=>v));
el.innerHTML="<table>"+e.map(([k,v])=>`<tr><td>${esc(k)}</td><td class="n">${v}</td><td style="width:50%"><div class="bar ${cls}" style="width:${v/m*100}%"></div></td></tr>`).join("")+"</table>"}
bars(D.actions,$("#kinds"),"k");bars(D.rejections,$("#rejects"),"");
let filter="acted";const tb=$("#traces");
function row(t){const stim=t.scripted?'<span class="empty">scripted command; no policy state logged</span>':`health ${t.health}<br>stock ${fmt(t.inventory)}<br><span style="color:var(--muted)">want ${fmt(t.targets)} · spare ${t.spendable??"-"} · ${t.open_offers??0} open offers</span>`;
const dec=(t.decision_id?`<div style="color:var(--muted);font-size:11px">${esc(t.decision_id)}${t.timing?` · decided in ${t.timing.decide_ms}ms`:""}</div>`:"")+(t.actions.length?t.actions.map(a=>`<span class="tag">${esc(a.kind)}</span>`+`<div style="color:var(--muted);font-size:12px">${esc(Object.entries(a).filter(([k])=>k!="kind").map(([k,v])=>k+"="+JSON.stringify(v)).join(" "))}</div>`).join(""):`<span class="empty">wait${t.wait_reason?": "+esc(t.wait_reason):""}</span>`);
const passed=Object.entries(t.passed||{});
const why=(t.reasons.length?`<ul class="reasons">${t.reasons.map(r=>`<li>${esc(r)}</li>`).join("")}</ul>`:"")+(passed.length?`<div style="font-size:12px;margin-top:4px">passed: ${passed.map(([o,r])=>`<div><b>${esc(o)}</b> <span style="color:var(--muted)">${esc(r)}</span></div>`).join("")}</div>`:"")+(t.skipped?`<span class="tag bad">${t.skipped} tick(s) skipped before this</span>`:"");
const out=t.outcomes.map(o=>`<span class="tag ${o.ok?"ok":"bad"}">${esc(o.code)}</span>${o.object_id?`<div style="font-size:12px">${esc(o.object_id)}</div>`:""}${o.inventory_after?`<div style="color:var(--muted);font-size:12px">after ${fmt(o.inventory_after)}</div>`:""}${o.notes.length?`<div style="color:var(--muted);font-size:12px">${esc(o.notes.join("; "))}</div>`:""}`).join("")||"";
return `<tr id="tick-${t.tick}"><td class="n">${t.tick??"-"}${shortTicks.has(t.tick)?'<br><span class="tag bad">short</span>':""}</td><td>${stim}</td><td>${dec}</td><td>${why}</td><td>${out}</td></tr>`}
function render(){const q=$("#q").value.toLowerCase();const rows=D.traces.filter(t=>{
if(filter=="acted"&&!t.actions.length)return false;if(filter=="rejected"&&!t.outcomes.some(o=>!o.ok))return false;if(filter=="short"&&!shortTicks.has(t.tick))return false;
return !q||JSON.stringify([t.reasons,t.actions,t.outcomes,t.passed,t.wait_reason,t.decision_id]).toLowerCase().includes(q)});
tb.innerHTML=rows.length?rows.map(row).join(""):'<tr><td colspan="5" class="empty">No ticks match.</td></tr>'}
document.querySelectorAll(".controls button").forEach(b=>b.addEventListener("click",()=>{filter=b.dataset.f;document.querySelectorAll(".controls button").forEach(x=>x.setAttribute("aria-pressed",x==b));render()}));
$("#q").addEventListener("input",render);
function jump(tick){if(!document.getElementById("tick-"+tick)){filter="all";document.querySelectorAll(".controls button").forEach(x=>x.setAttribute("aria-pressed",x.dataset.f=="all"));$("#q").value="";render()}
const r=document.getElementById("tick-"+tick);if(r){document.querySelectorAll("tr.sel").forEach(x=>x.classList.remove("sel"));r.classList.add("sel");r.scrollIntoView({block:"center",behavior:"smooth"})}}
render();
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evidence", type=Path, help="client evidence log (JSONL)")
    parser.add_argument("--every", type=int, default=10, help="timeline interval in ticks")
    parser.add_argument("--html", type=Path, help="also write an interactive HTML report here")
    parser.add_argument("--offer", help="instead, trace one offer: knew -> decided -> sent -> confirmed")
    args = parser.parse_args(argv)
    records = load(args.evidence)
    if args.offer:
        print("\n".join(offer_story(records, args.offer)))
        return 0
    print(report(records, args.every))
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(html_report(records, f"Run report: {args.evidence.name}"),
                             encoding="utf-8")
        print(f"\nwrote {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
