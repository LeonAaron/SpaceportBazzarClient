"""A human-readable panel of where our planet stands right now.

    P01 | tick 42/120 | RUNNING | participating | health 100 | strategy reserve-trader
    reserves     water 55 (spendable 35)   food 40/60 target   components 22/60 target
    pending      offer 20 WATER for 20 FOOD to P03   | 1 command awaiting confirmation
    open offers  out offer-12 -> P03  pay 20 water  get 20 food   expires t45
                 in  offer-19 <- P05  pay 0         get 3 food    expires t44
    recent trades t41 txn-9  P03  paid 20 water  got 20 food

Logged every few ticks and optionally rewritten to a file each tick; an `.html`
file refreshes itself, so a browser tab becomes a live view of the client.
"""

from __future__ import annotations

import html
from pathlib import Path

from bazaar_client.domain.types import Bundle, Resource, Snapshot
from bazaar_client.execution.evidence import offer_view, trade_view

RECENT_TRADES = 5


def _bundle(values: dict[str, int]) -> str:
    parts = [f"{n} {name}" for name, n in values.items() if n]
    return " + ".join(parts) or "0"


def _describe_action(action) -> str:
    details = " ".join(
        f"{key}={_bundle(value) if isinstance(value, dict) else value}"
        for key, value in action.describe().items()
    )
    return f"{action.kind} {details}".strip()


def render_status(
    snapshot: Snapshot,
    decision=None,
    *,
    status: str = "",
    strategy: str = "",
    in_flight: int = 0,
) -> str:
    me = snapshot.me
    rules = snapshot.rules
    lines = [
        " | ".join(p for p in (
            me.station_id,
            f"tick {snapshot.tick}/{rules.duration_ticks}",
            snapshot.phase.name,
            status,
            f"health {me.health}/{rules.max_health}" + (" FAILED" if me.failed_once else ""),
            f"strategy {strategy}" if strategy else "",
        ) if p)
    ]

    targets = decision.targets if decision is not None else Bundle.zero()
    reserves = []
    for resource in Resource:
        held = me.inventory.get(resource)
        if resource == me.specialty:
            spendable = f" (spendable {decision.spendable})" if decision is not None else ""
            reserves.append(f"{resource.name.lower()} {held}{spendable}")
        else:
            reserves.append(f"{resource.name.lower()} {held}/{targets.get(resource)} target")
    lines.append("reserves      " + "   ".join(reserves))

    actions = decision.actions if decision is not None else []
    pending = "; ".join(_describe_action(a) for a in actions) or "waiting"
    if in_flight:
        pending += f" | {in_flight} command(s) awaiting confirmation"
    lines.append("pending       " + pending)

    offers = [offer_view(o, me.station_id)
              for o in (*snapshot.outgoing_open_offers(), *snapshot.incoming_open_offers())]
    if not offers:
        lines.append("open offers   none")
    for i, o in enumerate(offers):
        outgoing = o["direction"] == "outgoing"
        arrow, side = ("->", "out") if outgoing else ("<-", "in ")
        label = "open offers   " if i == 0 else "              "
        lines.append(
            f"{label}{side} {o['offer_id']} {arrow} {o['counterparty']}  "
            f"pay {_bundle(o['we_pay'])}  get {_bundle(o['we_get'])}  expires t{o['expires_tick']}"
        )

    trades = sorted(snapshot.transactions, key=lambda t: t.settled_tick)[-RECENT_TRADES:]
    if not trades:
        lines.append("recent trades none yet")
    for i, txn in enumerate(reversed(trades)):
        t = trade_view(txn, me.station_id)
        label = "recent trades " if i == 0 else "              "
        lines.append(
            f"{label}t{t['settled_tick']} {t['transaction_id']}  {t['counterparty']}  "
            f"paid {_bundle(t['we_paid'])}  got {_bundle(t['we_got'])}"
        )
    return "\n".join(lines)


def write_status_file(path: Path, panel: str, refresh_s: int = 2) -> None:
    """Replace the file atomically so a reader never sees half a panel."""
    if path.suffix.lower() in (".html", ".htm"):
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta http-equiv='refresh' content='{refresh_s}'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Bazaar client status</title><style>"
            ":root{--bg:#f7f7f5;--ink:#1d1d1b}"
            "@media (prefers-color-scheme:dark){:root{--bg:#161615;--ink:#ecece8}}"
            "body{margin:0;background:var(--bg);color:var(--ink)}"
            "pre{font:14px/1.5 ui-monospace,Consolas,monospace;padding:16px;white-space:pre-wrap}"
            f"</style></head><body><pre>{html.escape(panel)}</pre></body></html>"
        )
    else:
        body = panel + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(path)
