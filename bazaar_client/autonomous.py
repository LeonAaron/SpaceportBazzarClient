"""The trading loop: observe, decide, act, record.

Decisions are made from the newest snapshot and executed one command at a time.
Nothing here chooses terms; that is entirely `policy.decide`. This module owns
only the plumbing between the session and that decision.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from bazaar_client.app import BazaarSession, CommandOutcome, SessionClosedError
from bazaar_client.config import ClientConfig
from bazaar_client.domain.types import Phase, ResultCode, Snapshot
from bazaar_client.execution.evidence import EvidenceLog
from bazaar_client.execution.executor import Executor
from bazaar_client.policy.decide import Decision, decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.world.commitments import CommitmentTracker
from bazaar_client.world.model import WorldModel

logger = logging.getLogger(__name__)

TERMINAL_PHASES = frozenset({Phase.FINISHED, Phase.ABORTED})


@dataclass
class TradingStats:
    decisions: int = 0
    commands_sent: int = 0
    commands_ok: int = 0
    commands_rejected: int = 0
    accepts: int = 0
    offers: int = 0
    gifts: int = 0
    advertisements: int = 0
    withdrawals: int = 0
    rejections_by_code: dict[str, int] = field(default_factory=dict)
    final_snapshot: Snapshot | None = None

    def record_rejection(self, code: str) -> None:
        self.rejections_by_code[code] = self.rejections_by_code.get(code, 0) + 1


class TradingLoop:
    def __init__(self, session: BazaarSession, evidence_path: Path | None = None) -> None:
        self._session = session
        self._world = WorldModel()
        self._commitments = CommitmentTracker()
        self._memory = PolicyMemory()
        self._evidence = EvidenceLog(evidence_path)
        self._executor = Executor(
            session, self._evidence, self._commitments, self._memory.counterparties
        )
        self.stats = TradingStats()

    @property
    def memory(self) -> PolicyMemory:
        return self._memory

    async def run(self, max_decisions: int | None = None) -> TradingStats:
        """Trade until the run ends, the connection drops, or a decision cap is hit."""
        snapshot, ack = await self._session.handshake()
        logger.info(
            "ready as %s: specialty %s, upkeep %s, %d planets in the directory",
            snapshot.self_station_id,
            snapshot.me.specialty.name,
            snapshot.me.upkeep_per_tick.as_dict(),
            len(snapshot.directory),
        )

        sequence = snapshot.snapshot_sequence
        while max_decisions is None or self.stats.decisions < max_decisions:
            try:
                snapshot = await self._session.wait_for_snapshot(min_sequence=sequence)
            except (SessionClosedError, asyncio.TimeoutError) as exc:
                logger.info("stopping: %s", exc)
                break

            sequence = snapshot.snapshot_sequence + 1
            self._world.apply(snapshot)
            self.stats.final_snapshot = snapshot

            await self.step(snapshot)

            if snapshot.phase in TERMINAL_PHASES:
                logger.info("run reached %s; no further trading", snapshot.phase.name)
                break

        self._report(self.stats.final_snapshot)
        return self.stats

    async def step(self, snapshot: Snapshot) -> Decision:
        """One observe-decide-act cycle against a single snapshot."""
        decision, self._memory = decide(snapshot, self._memory, self._commitments)
        self.stats.decisions += 1
        self._log_decision(snapshot, decision)

        for action in decision.actions:
            request_id = self._session.request_ids.next(action.kind)
            try:
                outcome = await self._executor.execute(
                    action, request_id, step=f"tick-{snapshot.tick}"
                )
            except (SessionClosedError, asyncio.TimeoutError) as exc:
                logger.warning("command %s did not complete: %s", request_id, exc)
                break
            self._record(action, outcome)
            self._executor.confirm(self._session.latest_snapshot)

        return decision

    def _record(self, action, outcome: CommandOutcome) -> None:
        self.stats.commands_sent += 1
        counters = {
            "accept": "accepts",
            "offer": "offers",
            "advertise": "advertisements",
            "withdraw": "withdrawals",
        }
        attribute = counters.get(action.kind)
        if attribute:
            setattr(self.stats, attribute, getattr(self.stats, attribute) + 1)
        if getattr(action, "is_gift", False):
            self.stats.gifts += 1

        if outcome.ok:
            self.stats.commands_ok += 1
            return

        self.stats.commands_rejected += 1
        self.stats.record_rejection(outcome.code_name)

        if outcome.result is not None and outcome.result.code is ResultCode.RATE_LIMITED:
            self._memory.block_until(outcome.result.retry_after_tick)

    def _log_decision(self, snapshot: Snapshot, decision: Decision) -> None:
        logger.info(
            "tick %d health %d inventory %s | reserve %s | %s",
            snapshot.tick,
            snapshot.me.health,
            snapshot.me.inventory.as_dict(),
            decision.reserve.as_dict(),
            ", ".join(f"{r.name}={u.name}" for r, u in decision.urgency.items()),
        )
        for reason in decision.reasons:
            logger.info("  -> %s", reason)

    def _report(self, snapshot: Snapshot | None) -> None:
        logger.info(
            "decisions=%d sent=%d ok=%d rejected=%d (accepts=%d offers=%d gifts=%d ads=%d)",
            self.stats.decisions,
            self.stats.commands_sent,
            self.stats.commands_ok,
            self.stats.commands_rejected,
            self.stats.accepts,
            self.stats.offers,
            self.stats.gifts,
            self.stats.advertisements,
        )
        if self.stats.rejections_by_code:
            logger.info("rejections: %s", self.stats.rejections_by_code)
        if snapshot is not None:
            logger.info(
                "final: health=%d inventory=%s shortage_ticks=%d fully_supplied_ticks=%d",
                snapshot.me.health,
                snapshot.me.inventory.as_dict(),
                snapshot.me.shortage_ticks,
                snapshot.me.fully_supplied_ticks,
            )


async def run_trading(
    config: ClientConfig,
    evidence_path: Path | None = None,
    max_decisions: int | None = None,
) -> TradingStats:
    async with BazaarSession(config) as session:
        return await TradingLoop(session, evidence_path).run(max_decisions)
