#!/usr/bin/env python3
"""Summarise our own client's evidence log (the --evidence-file JSONL).

Where analyze_run.py reads the Directorate's log, this reads what *we* recorded:
what the policy saw each tick, what it decided and why, what it sent, what the
server answered, and when the connection was up or down.

    python scripts/analyze_evidence.py logs/live-evidence.jsonl
    python scripts/analyze_evidence.py logs/live-evidence.jsonl --html logs/report.html

Record kinds in the log (one JSON object per line):
    {"kind": "decision", "timestamp", "tick", "health", "inventory", "available",
     "import_targets", "specialty_spendable", "open_outgoing_offers", "actions", "reasons"}
    {"step", "action_kind", "action", "timestamp", "request_id", "observed_tick",
     "result_ok", "result_code", "object_id", "inventory_after", "notes"}
    {"kind": "connection", "timestamp", "event", "detail"}
      event is connecting | connected | disconnected | reconnecting | closed

Standard library only, so any team can run it without this project installed.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

RESOURCES = ("water", "food", "components")
DOWN_EVENTS = ("disconnected", "reconnecting", "closed")


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
    return {
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
    if o["decisions"]:
        lines = [
            f"ticks {o['first_tick']}..{o['last_tick']}: {o['decisions']} decisions logged, "
            f"{o['acting_decisions']} acted",
            f"final health {o['final_health']}  stock (water,food,components) "
            f"{_fmt(o['final_inventory'])}",
        ]
        for start, end in decision_gaps(records):
            lines.append(f"no decisions logged for ticks {start}..{end} (down or stalled)")
    else:
        lines = ["no policy decisions logged (a scripted run records its commands only)"]
    lines += [
        f"commands: {o['commands_sent']} sent, {o['commands_ok']} ok, "
        f"{o['commands_rejected']} rejected; {o['trades_settled']} trades settled by our accepts",
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
            if p["state"] == "down":
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
        "outcomes": outcomes,
    }


def _page_data(records: list[dict]) -> dict:
    return {
        "overview": overview(records),
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
<p class="sub">Generated from the client's evidence log: what the policy saw, what it decided and why, and what the server answered.</p>
<section class="panel"><div class="kpis" id="kpis"></div></section>
<section class="panel"><h2>Health and stock by tick</h2><svg id="chart" viewBox="0 0 1000 300"></svg>
<div class="legend"><span><i style="background:var(--health)"></i>health</span><span><i style="background:var(--water)"></i>water</span><span><i style="background:var(--food)"></i>food</span><span><i style="background:var(--components)"></i>components</span><span>click a tick to jump to its decision</span></div></section>
<section class="panel"><h2>Connection</h2><div id="conn"></div></section>
<div class="grid2">
<section class="panel"><h2>Commands by kind</h2><div id="kinds"></div></section>
<section class="panel"><h2>Rejected or unanswered commands</h2><div id="rejects"></div></section>
</div>
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
const O=D.overview;
const k=[["Ticks",O.first_tick==null?"-":O.first_tick+"–"+O.last_tick],["Final health",O.final_health??"-"],["Commands sent",O.commands_sent],["Rejected",O.commands_rejected],["Trades settled",O.trades_settled],["Downtime",D.periods.length?dur(O.downtime_s):"n/a"],["First shortage",O.first_shortage_tick??"none"],["Health lost",O.health_lost]];
$("#kpis").innerHTML=k.map(([l,v])=>`<div class="kpi"><b>${esc(v)}</b><span>${l}</span></div>`).join("");
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
const dec=t.actions.length?t.actions.map(a=>`<span class="tag">${esc(a.kind)}</span>`+`<div style="color:var(--muted);font-size:12px">${esc(Object.entries(a).filter(([k])=>k!="kind").map(([k,v])=>k+"="+JSON.stringify(v)).join(" "))}</div>`).join(""):'<span class="empty">wait</span>';
const why=t.reasons.length?`<ul class="reasons">${t.reasons.map(r=>`<li>${esc(r)}</li>`).join("")}</ul>`:"";
const out=t.outcomes.map(o=>`<span class="tag ${o.ok?"ok":"bad"}">${esc(o.code)}</span>${o.object_id?`<div style="font-size:12px">${esc(o.object_id)}</div>`:""}${o.inventory_after?`<div style="color:var(--muted);font-size:12px">after ${fmt(o.inventory_after)}</div>`:""}${o.notes.length?`<div style="color:var(--muted);font-size:12px">${esc(o.notes.join("; "))}</div>`:""}`).join("")||"";
return `<tr id="tick-${t.tick}"><td class="n">${t.tick??"-"}${shortTicks.has(t.tick)?'<br><span class="tag bad">short</span>':""}</td><td>${stim}</td><td>${dec}</td><td>${why}</td><td>${out}</td></tr>`}
function render(){const q=$("#q").value.toLowerCase();const rows=D.traces.filter(t=>{
if(filter=="acted"&&!t.actions.length)return false;if(filter=="rejected"&&!t.outcomes.some(o=>!o.ok))return false;if(filter=="short"&&!shortTicks.has(t.tick))return false;
return !q||JSON.stringify([t.reasons,t.actions,t.outcomes]).toLowerCase().includes(q)});
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
    args = parser.parse_args(argv)
    records = load(args.evidence)
    print(report(records, args.every))
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(html_report(records, f"Run report: {args.evidence.name}"),
                             encoding="utf-8")
        print(f"\nwrote {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
