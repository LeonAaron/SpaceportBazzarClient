"""The trading loop: observe, decide, act, record.

Decisions are made from the newest snapshot and executed one command at a time.
Nothing here chooses terms; that is entirely `policy.decide`. This module owns
only the plumbing between the session and that decision.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from bazaar_client.app import (
    BazaarSession,
    CommandBlockedError,
    CommandOutcome,
    SessionAbortedError,
    SessionClosedError,
)
from bazaar_client.connection.throttle import CommandThrottle
from bazaar_client.connection.ws_client import SubprotocolNotSelected
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
    def __init__(
        self,
        session: BazaarSession,
        evidence_path: Path | None = None,
        memory: PolicyMemory | None = None,
        stats: TradingStats | None = None,
        evidence: EvidenceLog | None = None,
    ) -> None:
        self._session = session
        self._world = WorldModel()
        self._commitments = CommitmentTracker()
        # Carried across reconnects: what we learned about counterparties and
        # how fast the market settles is still true on a new connection.
        self._memory = memory or PolicyMemory()
        self._evidence = evidence or EvidenceLog(evidence_path)
        self._executor = Executor(
            session, self._evidence, self._commitments, self._memory.counterparties
        )
        self.stats = stats or TradingStats()

    @property
    def memory(self) -> PolicyMemory:
        return self._memory

    async def run(self, max_decisions: int | None = None) -> TradingStats:
        """Trade until the run ends, the connection drops, or a decision cap is hit."""
        snapshot, ack = await self._session.handshake()
        if self._memory.run_id is not None and self._memory.run_id != snapshot.run_id:
            raise SessionAbortedError("run changed on reconnect; restart with fresh policy memory")
        self._memory.run_id = snapshot.run_id
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
        """Choose one action, then wait for its authoritative confirmation."""
        latest = self._session.latest_snapshot
        if latest is not None and latest.snapshot_sequence > snapshot.snapshot_sequence:
            snapshot = latest
        decision, self._memory = decide(
            snapshot, self._memory, self._commitments,
            command_budget=min(1, self._session.remaining_command_budget),
        )
        self.stats.decisions += 1
        self._log_decision(snapshot, decision)

        for action in decision.actions:
            request_id = self._session.request_ids.next(action.kind)
            try:
                outcome = await self._executor.execute(
                    action, request_id, step=f"tick-{snapshot.tick}"
                )
            except CommandBlockedError as exc:
                logger.info("command %s deferred: %s", request_id, exc)
                break
            self._record(action, outcome)
            self.stats.final_snapshot = self._session.latest_snapshot

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


def backoff_delay(attempt: int, cap: float) -> float:
    """Exponential with jitter, so repeated drops do not hammer the server."""
    base = min(cap, 1.0 * (2 ** max(0, attempt - 1)))
    return base * random.uniform(0.8, 1.2)


async def run_trading(
    config: ClientConfig,
    evidence_path: Path | None = None,
    max_decisions: int | None = None,
    max_attempts: int | None = None,
    sleep=asyncio.sleep,
) -> TradingStats:
    """Trade, reconnecting when the connection drops.

    A run lasts many ticks and the planet keeps consuming upkeep throughout, so
    a dropped connection has to be recovered rather than ending the run. Each
    new connection repeats the readiness exchange; policy memory carries over,
    while in-flight commitments do not, since their fate is unknown.
    """
    stats = TradingStats()
    memory = PolicyMemory()
    evidence = EvidenceLog(evidence_path)
    attempt = 0
    throttle = CommandThrottle(limit_per_tick=1)

    while max_attempts is None or attempt < max_attempts:
        attempt += 1
        try:
            async with BazaarSession(config, throttle=throttle) as session:
                loop = TradingLoop(
                    session, memory=memory, stats=stats, evidence=evidence
                )
                await loop.run(max_decisions)

                if session.abort_reason is not None:
                    logger.error("not reconnecting: %s", session.abort_reason)
                    return stats
                if stats.final_snapshot is not None and (
                    stats.final_snapshot.phase in TERMINAL_PHASES
                ):
                    return stats
                if max_decisions is not None and stats.decisions >= max_decisions:
                    return stats
        except SessionAbortedError as exc:
            logger.error("not reconnecting: %s", exc)
            return stats
        except (OSError, SessionClosedError, asyncio.TimeoutError) as exc:
            logger.warning("connection problem: %s", exc)
        except SubprotocolNotSelected:
            raise  # a misconfigured client, not a transient fault

        delay = backoff_delay(attempt, config.reconnect_max_backoff_s)
        logger.info("reconnecting in %.1fs (attempt %d)", delay, attempt)
        await sleep(delay)

    return stats
