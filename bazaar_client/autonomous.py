"""The trading loop: observe, decide, act, record.

Decisions are made from the newest snapshot and executed one command at a time.
Nothing here chooses terms; that is entirely the configured `Strategy`. This
module owns only the plumbing between the session and that decision, and the
measurements around it: how long a state waited, how long the decision took,
whether the client is participating, waiting by design, or stale.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from bazaar_client.app import (
    BazaarSession,
    CommandBlockedError,
    CommandOutcome,
    SessionAbortedError,
    SessionClosedError,
)
from bazaar_client.config import ClientConfig
from bazaar_client.connection.throttle import CommandThrottle
from bazaar_client.diagnostics import diagnose
from bazaar_client.domain.types import Phase, ResultCode, Snapshot
from bazaar_client.execution.evidence import EvidenceLog
from bazaar_client.execution.executor import Executor
from bazaar_client.metrics import LatencyRecorder
from bazaar_client.policy.decide import Decision
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.status import ClientStatus, StatusTracker
from bazaar_client.status_view import render_status, write_status_file
from bazaar_client.strategy import DEFAULT_STRATEGY, Strategy, get_strategy
from bazaar_client.version import build_info, describe_build
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
    # Lives here, not on TradingLoop, so reconnects don't re-log the same tick.
    last_logged_tick: int | None = None
    last_panel_tick: int | None = None

    def record_rejection(self, code: str) -> None:
        self.rejections_by_code[code] = self.rejections_by_code.get(code, 0) + 1

    def as_dict(self) -> dict:
        return {
            "decisions": self.decisions,
            "commands_sent": self.commands_sent,
            "commands_ok": self.commands_ok,
            "commands_rejected": self.commands_rejected,
            "accepts": self.accepts,
            "offers": self.offers,
            "gifts": self.gifts,
            "advertisements": self.advertisements,
            "withdrawals": self.withdrawals,
            "rejections_by_code": dict(self.rejections_by_code),
        }


def _ms_since(start: float) -> float:
    """perf_counter, not monotonic: monotonic ticks in ~16 ms steps on Windows."""
    return round((time.perf_counter() - start) * 1000, 3)


class TradingLoop:
    def __init__(
        self,
        session: BazaarSession,
        evidence_path: Path | None = None,
        memory: PolicyMemory | None = None,
        stats: TradingStats | None = None,
        evidence: EvidenceLog | None = None,
        *,
        strategy: Strategy | None = None,
        status: StatusTracker | None = None,
        latency: LatencyRecorder | None = None,
        session_number: int = 1,
        status_every: int = 0,
        status_file: Path | None = None,
        decide_in_thread: bool = False,
    ) -> None:
        self._session = session
        self._world = WorldModel()
        self._commitments = CommitmentTracker()
        # Carried across reconnects: what we learned about counterparties and
        # how fast the market settles is still true on a new connection.
        self._memory = memory or PolicyMemory()
        self._evidence = evidence or EvidenceLog(evidence_path)
        self._strategy = strategy or get_strategy(DEFAULT_STRATEGY)
        self._status = status or StatusTracker()
        self._latency = latency or LatencyRecorder()
        self._executor = Executor(
            session, self._evidence, self._commitments, self._memory.counterparties, self._latency
        )
        self.stats = stats or TradingStats()
        self._session_number = session_number
        self._status_every = status_every
        self._status_file = status_file
        # A thread keeps the socket reader running while a slow strategy thinks.
        self._decide_in_thread = decide_in_thread

    @property
    def memory(self) -> PolicyMemory:
        return self._memory

    @property
    def latency(self) -> LatencyRecorder:
        return self._latency

    async def run(self, max_decisions: int | None = None) -> TradingStats:
        """Trade until the run ends, the connection drops, or a decision cap is hit."""
        snapshot = await self._session.wait_for_snapshot(min_sequence=1)
        self._status.set(
            ClientStatus.AUTHENTICATED,
            f"token accepted; first state as {snapshot.self_station_id} in run {snapshot.run_id}",
        )
        ack = await self._session.wait_for_readiness()
        self._status.set(
            ClientStatus.SYNCHRONIZED, f"readiness confirmed at sequence {ack.snapshot_sequence}"
        )
        if self._memory.run_id is not None and self._memory.run_id != snapshot.run_id:
            raise SessionAbortedError("run changed on reconnect; restart with fresh policy memory")
        self._memory.run_id = snapshot.run_id
        logger.info(
            "ready as %s in run %s (client build %s, strategy %s): specialty %s, upkeep %s, "
            "%d planets in the directory",
            snapshot.self_station_id,
            snapshot.run_id,
            describe_build(),
            self._strategy.name,
            snapshot.me.specialty.name,
            snapshot.me.upkeep_per_tick.as_dict(),
            len(snapshot.directory),
        )

        sequence = snapshot.snapshot_sequence
        while max_decisions is None or self.stats.decisions < max_decisions:
            try:
                snapshot = await self._session.wait_for_snapshot(
                    min_sequence=sequence, timeout=self._status.poll_interval_s
                )
            except asyncio.TimeoutError:
                # A quiet server is not a dead connection: before a run starts
                # nothing may change for minutes. Dropping and reconnecting here
                # is what made run 2's P01 connect ten times at tick 0. A socket
                # that really dies ends the wait with SessionClosedError instead.
                if not self._status.check_stale():
                    logger.debug("no new state yet; staying connected and waiting")
                continue
            except SessionClosedError as exc:
                logger.info("stopping: %s", exc)
                break

            sequence = snapshot.snapshot_sequence + 1
            self._world.apply(snapshot)
            self.stats.final_snapshot = snapshot
            self._status.on_state(snapshot.phase, snapshot.rules, snapshot.tick)

            await self.step(snapshot)

            if snapshot.phase in TERMINAL_PHASES:
                logger.info("run reached %s; no further trading", snapshot.phase.name)
                break

        self._report(self.stats.final_snapshot)
        return self.stats

    async def _decide(self, snapshot: Snapshot, budget: int) -> Decision:
        args = (snapshot, self._memory, self._commitments)
        if self._decide_in_thread:
            decision, self._memory = await asyncio.to_thread(
                self._strategy.decide, *args, command_budget=budget
            )
        else:
            decision, self._memory = self._strategy.decide(*args, command_budget=budget)
        return decision

    async def step(self, snapshot: Snapshot) -> Decision:
        """Choose one action, then wait for its authoritative confirmation."""
        latest = self._session.latest_snapshot
        if latest is not None and latest.snapshot_sequence > snapshot.snapshot_sequence:
            snapshot = latest

        received = self._session.received_at(snapshot.snapshot_sequence)
        queue_ms = _ms_since(received) if received is not None else None
        budget = min(1, self._session.remaining_command_budget)
        started = time.perf_counter()
        decision = await self._decide(snapshot, budget)
        decide_ms = _ms_since(started)
        newest = self._session.latest_snapshot
        arrived = newest.snapshot_sequence - snapshot.snapshot_sequence if newest else 0

        if queue_ms is not None:
            self._latency.record("queue", queue_ms)
        self._latency.record("decide", decide_ms)
        self.stats.decisions += 1

        context = {
            "session": self._session_number,
            "strategy": self._strategy.name,
            "command_budget": budget,
            "timing": {"queue_ms": queue_ms, "decide_ms": decide_ms},
            "states_during_decision": arrived,
        }
        if not decision.actions and snapshot.phase is Phase.RUNNING:
            context["wait_reason"] = (
                "no command slot is available this tick" if budget <= 0
                else "nothing met the strategy's criteria to act"
            )
        decision_id = self._log_decision(snapshot, decision, context)
        self._show_status(snapshot, decision)

        for action in decision.actions:
            request_id = self._session.request_ids.next(action.kind)
            try:
                outcome = await self._executor.execute(
                    action, request_id, step=f"tick-{snapshot.tick}", decision_id=decision_id
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

    def _log_decision(
        self, snapshot: Snapshot, decision: Decision, context: dict | None = None
    ) -> str | None:
        """Record the decision; returns the id its commands carry, or None if skipped.

        Several snapshots arrive per tick; record each tick once, plus every
        decision that actually acted, so the log stays readable.
        """
        if not decision.actions and snapshot.tick == self.stats.last_logged_tick:
            return None
        context = dict(context or {})
        previous = self.stats.last_logged_tick
        if previous is not None and snapshot.tick > previous + 1:
            # Ticks that passed with no decision at all: evidence of a stall
            # when the connection was up the whole time.
            context["ticks_skipped"] = snapshot.tick - previous - 1
        self.stats.last_logged_tick = snapshot.tick
        passes = self._strategy.explain_passes(snapshot, decision)
        decision_id = self._evidence.decision(snapshot, decision, context=context, passes=passes)
        logger.info(
            "tick %d health %d inventory %s | import targets %s | spare %s %d",
            snapshot.tick,
            snapshot.me.health,
            snapshot.me.inventory.as_dict(),
            decision.targets.as_dict(),
            snapshot.me.specialty.name,
            decision.spendable,
        )
        for reason in decision.reasons:
            logger.info("  -> %s", reason)
        if "wait_reason" in context:
            logger.info("  -> waiting: %s", context["wait_reason"])
        return decision_id

    def _show_status(self, snapshot: Snapshot, decision: Decision) -> None:
        due = (self._status_every > 0 and snapshot.tick % self._status_every == 0
               and snapshot.tick != self.stats.last_panel_tick)
        if not due and self._status_file is None:
            return
        panel = render_status(
            snapshot, decision, status=self._status.current.value,
            strategy=self._strategy.name, in_flight=self._commitments.inflight_count,
        )
        if due:
            self.stats.last_panel_tick = snapshot.tick
            for line in panel.splitlines():
                logger.info("| %s", line)
        if self._status_file is not None:
            try:
                write_status_file(self._status_file, panel)
            except OSError as exc:
                logger.warning("could not update status file %s: %s", self._status_file, exc)

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
        for line in self._latency.report_lines():
            logger.info("latency %s", line)
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
    while in-flight commitments do not, since their fate is unknown. Only a
    network failure is retried: a configuration, authentication or protocol
    failure would fail the same way again, so it ends the run with a diagnosis.
    """
    stats = TradingStats()
    memory = PolicyMemory()
    evidence = EvidenceLog(evidence_path)
    strategy = get_strategy(config.strategy)
    latency = LatencyRecorder()
    status = StatusTracker(
        on_change=lambda previous, new, detail: evidence.status_event(
            new.value, previous=previous.value, detail=detail
        )
    )
    info = build_info()
    evidence.run_start(
        **info, strategy=strategy.name, mode=config.mode, station_id=config.station_id,
        ws_url=config.ws_url, max_decisions=max_decisions,
    )
    evidence.status_event(ClientStatus.STARTING.value, detail="process running")
    logger.info("client build %s, strategy %s", describe_build(info), strategy.name)
    attempt = 0
    session_number = 0
    throttle = CommandThrottle(limit_per_tick=1)

    try:
        while max_attempts is None or attempt < max_attempts:
            attempt += 1
            session_number += 1
            decisions_before = stats.decisions
            status.set(ClientStatus.CONNECTING, f"attempt {attempt}")
            evidence.connection_event("connecting", detail=f"attempt {attempt}")
            try:
                async with BazaarSession(config, throttle=throttle) as session:
                    status.set(ClientStatus.CONNECTED, "socket open, subprotocol confirmed")
                    evidence.connection_event("connected", detail=f"session {session_number}")
                    loop = TradingLoop(
                        session, memory=memory, stats=stats, evidence=evidence,
                        strategy=strategy, status=status, latency=latency,
                        session_number=session_number, status_every=config.status_every,
                        status_file=config.status_file, decide_in_thread=True,
                    )
                    await loop.run(max_decisions)

                    if stats.decisions > decisions_before:
                        # That connection worked; a later drop should retry quickly,
                        # not inherit the long delays of a string of failures.
                        attempt = 0

                    if session.abort_reason is not None:
                        diagnosis = diagnose(SessionAbortedError(
                            session.abort_reason, getattr(session, "abort_code", None)
                        ))
                        logger.error("not reconnecting: %s", diagnosis)
                        evidence.connection_event(
                            "closed", detail=f"aborted: {session.abort_reason}",
                            category=diagnosis.kind.value,
                        )
                        return stats
                    if stats.final_snapshot is not None and (
                        stats.final_snapshot.phase in TERMINAL_PHASES
                    ):
                        evidence.connection_event(
                            "closed", detail=f"run {stats.final_snapshot.phase.name}"
                        )
                        return stats
                    if max_decisions is not None and stats.decisions >= max_decisions:
                        evidence.connection_event("closed", detail="decision cap reached")
                        return stats
            except SessionAbortedError as exc:
                diagnosis = diagnose(exc)
                logger.error("not reconnecting: %s", diagnosis)
                status.set(ClientStatus.DISCONNECTED, diagnosis.summary)
                evidence.connection_event(
                    "closed", detail=f"aborted: {exc}", category=diagnosis.kind.value
                )
                return stats
            except Exception as exc:
                diagnosis = diagnose(exc)
                status.set(ClientStatus.DISCONNECTED, diagnosis.summary)
                if not diagnosis.retryable:
                    evidence.connection_event(
                        "failed", detail=diagnosis.summary, category=diagnosis.kind.value
                    )
                    raise
                logger.warning("connection problem %s", diagnosis)
                evidence.connection_event(
                    "disconnected", detail=diagnosis.summary, category=diagnosis.kind.value
                )

            status.set(ClientStatus.DISCONNECTED, "connection ended")
            delay = backoff_delay(attempt, config.reconnect_max_backoff_s)
            logger.info("reconnecting in %.1fs (attempt %d)", delay, attempt)
            evidence.connection_event("reconnecting", detail=f"delay={delay:.1f}s")
            await sleep(delay)
    finally:
        evidence.run_end(
            **stats.as_dict(),
            latency={name: s.as_dict() for name, s in latency.summary().items()},
            deadline_checks=latency.deadline_checks,
            missed_deadlines=latency.missed_deadlines,
            status_seconds=status.durations(),
            final_status=status.current.value,
        )

    return stats
