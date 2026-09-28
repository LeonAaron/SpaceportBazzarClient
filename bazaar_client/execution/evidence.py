"""Decision evidence as JSONL: one self-contained JSON object per line.

Record kinds, in the order a run produces them:

  run_start    once per process: build, strategy and configuration (never secrets)
  status       a status transition, e.g. synchronized -> participating
  connection   connecting / connected / disconnected / reconnecting / closed
  decision     what the strategy saw, what it chose and why -- including a
               decision to wait, and why each incoming offer was passed over
  (command)    one per command sent: the state it came from, the result, the
               state that confirmed it, and how long each step took
  run_end      final counts, latency percentiles and time spent in each status

Identifiers that connect them: `run_id` (the server's run), `session` (which
connection of this process), `decision_id` (links a decision to the commands it
caused), `request_id` (links a command to its result), and the server's
`object_id` / `transaction_id` (link a command to the offer and the trade).

Each record is appended and the file closed at once, so everything written
survives a crash. An existing log is renamed rather than overwritten when a new
process starts, so a restart after a crash keeps the evidence of what led to it.
Tokens never reach here: only game data is recorded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bazaar_client.domain.types import Offer, Snapshot, Transaction

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class EvidenceRecord:
    step: str
    action_kind: str
    action: dict
    timestamp: str = field(default_factory=_now)
    decision_id: str | None = None
    request_id: str | None = None
    observed_tick: int | None = None
    observed_world_version: int | None = None
    observed_snapshot_sequence: int | None = None
    result_ok: bool | None = None
    result_code: str | None = None
    object_id: str | None = None
    transaction_id: str | None = None
    processed_tick: int | None = None
    deadline_missed: bool | None = None
    response_ms: float | None = None
    confirm_ms: float | None = None
    confirmed_world_version: int | None = None
    confirmed_snapshot_sequence: int | None = None
    inventory_after: dict | None = None
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> str:
        return json.dumps({k: v for k, v in self.__dict__.items() if v is not None})


def offer_view(offer: Offer, me: str) -> dict[str, Any]:
    """An open offer from our side of the table: who with, what we pay and get."""
    outgoing = offer.proposer_id == me
    return {
        "offer_id": offer.offer_id,
        "direction": "outgoing" if outgoing else "incoming",
        "counterparty": offer.recipient_id if outgoing else offer.proposer_id,
        "we_pay": offer.what_station_pays(me).as_dict(),
        "we_get": offer.what_station_receives(me).as_dict(),
        "expires_tick": offer.expires_tick,
    }


def trade_view(txn: Transaction, me: str) -> dict[str, Any]:
    outgoing = txn.proposer_id == me
    return {
        "transaction_id": txn.transaction_id,
        "offer_id": txn.offer_id,
        "counterparty": txn.recipient_id if outgoing else txn.proposer_id,
        "we_paid": (txn.give if outgoing else txn.receive).as_dict(),
        "we_got": (txn.receive if outgoing else txn.give).as_dict(),
        "settled_tick": txn.settled_tick,
    }


def rotate(path: Path) -> Path | None:
    """Move a previous run's non-empty log aside, named by when it was last written."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.stem}.{stamp}-{counter}{path.suffix}")
        counter += 1
    path.rename(target)
    return target


class EvidenceLog:
    """Appends records to a JSONL file and keeps them in memory for assertions."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._records: list[EvidenceRecord] = []
        self._decisions: list[dict[str, Any]] = []
        self._decision_count = 0
        self._seen_transactions: set[str] = set()
        self.rotated_to: Path | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.rotated_to = rotate(path)
            path.write_text("")
            if self.rotated_to is not None:
                logger.info("previous evidence log kept as %s", self.rotated_to)

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def records(self) -> list[EvidenceRecord]:
        return list(self._records)

    @property
    def decisions(self) -> list[dict[str, Any]]:
        return list(self._decisions)

    # --- run framing ------------------------------------------------------

    def run_start(self, **fields: Any) -> None:
        self._write_entry({"kind": "run_start", **fields})

    def run_end(self, **fields: Any) -> None:
        self._write_entry({"kind": "run_end", **fields})

    def status_event(self, status: str, *, previous: str | None = None, detail: str = "") -> None:
        entry = {"kind": "status", "status": status}
        if previous is not None:
            entry["previous"] = previous
        if detail:
            entry["detail"] = detail
        self._write_entry(entry)

    def connection_event(
        self, event: str, *, detail: str | None = None, category: str | None = None
    ) -> None:
        """connecting / connected / disconnected / reconnecting / closed, so downtime is visible."""
        entry = {"kind": "connection", "event": event}
        if detail is not None:
            entry["detail"] = detail
        if category is not None:
            entry["category"] = category
        self._write_entry(entry)

    # --- decisions --------------------------------------------------------

    def decision(
        self,
        snapshot: Snapshot,
        decision,
        *,
        context: dict[str, Any] | None = None,
        passes: dict[str, str] | None = None,
    ) -> str:
        """What the strategy saw and chose, so a later outcome can be explained.

        Returns the decision id that the commands it causes will carry.
        """
        self._decision_count += 1
        decision_id = f"d{self._decision_count}"
        me = snapshot.self_station_id
        open_offers = [*snapshot.outgoing_open_offers(), *snapshot.incoming_open_offers()]
        new_trades = [
            trade_view(t, me) for t in snapshot.transactions
            if t.transaction_id not in self._seen_transactions
        ]
        self._seen_transactions.update(t["transaction_id"] for t in new_trades)
        entry: dict[str, Any] = {
            "kind": "decision",
            "timestamp": _now(),
            "decision_id": decision_id,
            "run_id": snapshot.run_id,
            "tick": snapshot.tick,
            "snapshot_sequence": snapshot.snapshot_sequence,
            "phase": snapshot.phase.name,
            "health": snapshot.me.health,
            "inventory": snapshot.me.inventory.as_dict(),
            "available": decision.available.as_dict(),
            "import_targets": decision.targets.as_dict(),
            "specialty_spendable": decision.spendable,
            "open_outgoing_offers": len(snapshot.outgoing_open_offers()),
            "open_offers": [offer_view(o, me) for o in open_offers],
            "new_transactions": new_trades,
            "verdict": "act" if decision.actions else "wait",
            "actions": [{"kind": a.kind, **a.describe()} for a in decision.actions],
            "reasons": list(decision.reasons),
        }
        if passes:
            entry["passed_offers"] = passes
        if context:
            entry.update(context)
        self._decisions.append(entry)
        self._write_line(json.dumps(entry))
        return decision_id

    # --- commands ---------------------------------------------------------

    def start(
        self, step: str, action, observed: Snapshot | None, decision_id: str | None = None
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            step=step,
            action_kind=action.kind,
            action=action.describe(),
            decision_id=decision_id,
            observed_tick=observed.tick if observed else None,
            observed_world_version=observed.world_version if observed else None,
            observed_snapshot_sequence=observed.snapshot_sequence if observed else None,
        )
        self._records.append(record)
        return record

    def note(self, record: EvidenceRecord, message: str) -> None:
        record.notes.append(message)

    def complete(self, record: EvidenceRecord, confirmed: Snapshot | None = None) -> None:
        if confirmed is not None:
            record.confirmed_world_version = confirmed.world_version
            record.confirmed_snapshot_sequence = confirmed.snapshot_sequence
            record.inventory_after = confirmed.me.inventory.as_dict()
        self._write_line(record.as_json())

    # --- file -------------------------------------------------------------

    def _write_entry(self, entry: dict[str, Any]) -> None:
        self._write_line(json.dumps({"kind": entry.pop("kind"), "timestamp": _now(), **entry}))

    def _write_line(self, line: str) -> None:
        logger.debug("evidence %s", line)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read_back(self) -> list[dict[str, Any]]:
        if self._path is None:
            return [json.loads(r.as_json()) for r in self._records]
        return [
            json.loads(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
