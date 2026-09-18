"""Decision evidence as JSONL.

One record per action links the state it was decided from, the command sent,
the answer, and the snapshot that confirmed it -- enough to explain an outcome
afterwards. Tokens never reach here: only game data is recorded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bazaar_client.domain.types import Snapshot

logger = logging.getLogger(__name__)


@dataclass
class EvidenceRecord:
    step: str
    action_kind: str
    action: dict
    request_id: str | None = None
    observed_tick: int | None = None
    observed_world_version: int | None = None
    observed_snapshot_sequence: int | None = None
    result_ok: bool | None = None
    result_code: str | None = None
    object_id: str | None = None
    transaction_id: str | None = None
    confirmed_world_version: int | None = None
    confirmed_snapshot_sequence: int | None = None
    inventory_after: dict | None = None
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> str:
        return json.dumps({k: v for k, v in self.__dict__.items() if v is not None})


class EvidenceLog:
    """Appends records to a JSONL file and keeps them in memory for assertions."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._records: list[EvidenceRecord] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    @property
    def records(self) -> list[EvidenceRecord]:
        return list(self._records)

    def start(self, step: str, action, observed: Snapshot | None) -> EvidenceRecord:
        record = EvidenceRecord(
            step=step,
            action_kind=action.kind,
            action=action.describe(),
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
        self._write(record)

    def _write(self, record: EvidenceRecord) -> None:
        line = record.as_json()
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
