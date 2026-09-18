"""Replays the practice server's scripted ten-step exchange.

This is a fixed script, not a trading policy: it exists to prove the client
builds every command correctly, reads every answer, and tracks state through a
complete exchange. The expected counters come from the exercise guide and hold
for one connection with no extra syncs or retries.

The decision policy replaces this driver in the next step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from bazaar_client.app import BazaarSession
from bazaar_client.config import ClientConfig
from bazaar_client.domain.types import (
    Bundle,
    ControlCode,
    OfferStatus,
    Resource,
    Snapshot,
)
from bazaar_client.execution.actions import (
    AcceptAction,
    AdvertiseAction,
    OfferAction,
    WithdrawAction,
)
from bazaar_client.execution.evidence import EvidenceLog
from bazaar_client.execution.executor import Executor
from bazaar_client.world.commitments import CommitmentTracker
from bazaar_client.world.counterparties import CounterpartyModel
from bazaar_client.world.model import WorldModel

logger = logging.getLogger(__name__)

PEER = "P02"
TTL = 6

# Request ids from the exercise guide; reusing one only ever retries that command.
ADVERTISE_1 = "student-advertise-1"
ADVERTISE_SEEKING = "student-advertise-seeking-1"
OFFER_1 = "student-offer-1"
ACCEPT_1 = "student-accept-1"
WITHDRAW_1 = "student-withdraw-1"
ADVERTISE_2 = "student-advertise-2"

EXPECTED_MESSAGES_SENT = 8
EXPECTED_FINAL_INVENTORY = Bundle(28, 31, 31)


@dataclass
class Check:
    step: str
    description: str
    passed: bool
    detail: str = ""


@dataclass
class WalkthroughResult:
    checks: list[Check] = field(default_factory=list)
    messages_sent: int = 0
    final_inventory: Bundle | None = None
    transaction_count: int = 0
    stored_results: int = 0

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def record(self, step: str, description: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(step, description, passed, detail))
        level = logging.INFO if passed else logging.ERROR
        logger.log(
            level, "[%s] %s %s%s", step, "PASS" if passed else "FAIL", description,
            f" -- {detail}" if detail else "",
        )

    def expect(self, step: str, description: str, actual, expected) -> None:
        self.record(step, description, actual == expected, f"expected {expected}, got {actual}")


class ScriptedWalkthrough:
    def __init__(self, session: BazaarSession, evidence_path: Path | None = None) -> None:
        self._session = session
        self._world = WorldModel()
        self._commitments = CommitmentTracker()
        self._counterparties = CounterpartyModel()
        self._evidence = EvidenceLog(evidence_path)
        self._executor = Executor(
            session, self._evidence, self._commitments, self._counterparties
        )
        self._result = WalkthroughResult()
        self._sent = 0

    @property
    def evidence(self) -> EvidenceLog:
        return self._evidence

    def _observe(self, snapshot: Snapshot) -> Snapshot:
        self._world.apply(snapshot)
        self._counterparties.update(snapshot)
        return snapshot

    async def _await_state(self, sequence: int) -> Snapshot:
        """Fetch one documented state by sequence.

        Steps 4-6 deliver three states with no command from us, so they can land
        together; each must still be checked individually.
        """
        snapshot = await self._session.wait_for_sequence(sequence)
        return self._observe(snapshot)

    async def run(self) -> WalkthroughResult:
        await self._step_1_ready()
        await self._step_2_advertise()
        advertisement_id = await self._step_3_replace_advertisement()
        await self._step_4_offer()
        await self._step_5_observe_acceptance()
        gift_offer_id = await self._step_6_observe_gift()
        if not gift_offer_id:
            # Sending an accept with no offer would end the exercise in a
            # scenario mismatch, so stop and report the missing gift instead.
            self._result.messages_sent = self._sent
            return self._result
        await self._step_7_accept_gift(gift_offer_id)
        await self._step_8_withdraw(advertisement_id)
        await self._step_9_request_limit()
        await self._step_10_sync()

        self._result.messages_sent = self._sent
        self._result.expect(
            "totals", "messages sent", self._sent, EXPECTED_MESSAGES_SENT
        )
        return self._result

    # --- steps ------------------------------------------------------------

    async def _step_1_ready(self) -> None:
        snapshot, ack = await self._session.handshake()
        self._observe(snapshot)
        self._sent += 1  # ready

        r = self._result
        r.expect("1", "opening world_version", snapshot.world_version, 2)
        r.expect("1", "opening snapshot_sequence", snapshot.snapshot_sequence, 1)
        r.expect("1", "station is P01", snapshot.self_station_id, "P01")
        r.expect("1", "opening inventory", snapshot.me.inventory, Bundle(30, 30, 30))
        r.expect("1", "specialty", snapshot.me.specialty, Resource.WATER)
        r.expect("1", "readiness acknowledged", (ack.ready, ack.snapshot_sequence), (True, 1))
        r.expect("1", "readiness run id matches", ack.run_id, snapshot.run_id)

        peer_ads = [a for a in snapshot.advertisements if a.station_id == PEER]
        r.record(
            "1",
            "P02 advertises selling food, seeking water",
            len(peer_ads) == 1
            and peer_ads[0].selling == frozenset({Resource.FOOD})
            and peer_ads[0].seeking == frozenset({Resource.WATER}),
            str([(sorted(x.name for x in a.selling), sorted(x.name for x in a.seeking)) for a in peer_ads]),
        )

    async def _step_2_advertise(self) -> None:
        action = AdvertiseAction(
            frozenset({Resource.WATER}), frozenset({Resource.FOOD}), TTL
        )
        outcome = await self._executor.execute(action, ADVERTISE_1, step="2")
        self._sent += 1

        r = self._result
        r.record("2", "advertise succeeded", outcome.ok, outcome.code_name)
        r.expect("2", "result request_id echoes ours", outcome.result.request_id, ADVERTISE_1)

        state = await self._await_state(2)
        self._executor.confirm(state)
        r.expect("2", "world_version", state.world_version, 3)
        r.expect("2", "snapshot_sequence", state.snapshot_sequence, 2)
        r.record(
            "2",
            "our advertisement is published",
            state.own_advertisement() is not None,
        )
        r.expect(
            "2", "advertising moves no resources", state.me.inventory, Bundle(30, 30, 30)
        )

    async def _step_3_replace_advertisement(self) -> str:
        action = AdvertiseAction(frozenset(), frozenset({Resource.COMPONENTS}), TTL)
        outcome = await self._executor.execute(action, ADVERTISE_SEEKING, step="3")
        self._sent += 1

        r = self._result
        r.record("3", "replacement advertise succeeded", outcome.ok, outcome.code_name)

        state = await self._await_state(3)
        self._executor.confirm(state)
        r.expect("3", "world_version", state.world_version, 4)
        r.expect("3", "snapshot_sequence", state.snapshot_sequence, 3)

        own = state.own_advertisement()
        r.record(
            "3",
            "replacement sells nothing and seeks components",
            own is not None
            and own.selling == frozenset()
            and own.seeking == frozenset({Resource.COMPONENTS}),
        )
        r.record(
            "3",
            "at most one active advertisement per station",
            len([a for a in state.advertisements if a.station_id == "P01"]) == 1,
        )
        r.expect("3", "inventory unchanged", state.me.inventory, Bundle(30, 30, 30))

        advertisement_id = outcome.result.object_id
        r.record("3", "result carries the advertisement id", advertisement_id is not None)
        return advertisement_id

    async def _step_4_offer(self) -> None:
        action = OfferAction(PEER, Bundle(water=2), Bundle(food=1), TTL)
        outcome = await self._executor.execute(action, OFFER_1, step="4")
        self._sent += 1

        r = self._result
        r.record("4", "offer succeeded", outcome.ok, outcome.code_name)

        state = await self._await_state(4)
        self._executor.confirm(state)
        r.expect("4", "world_version", state.world_version, 5)
        r.expect("4", "snapshot_sequence", state.snapshot_sequence, 4)

        ours = [o for o in state.offers if o.proposer_id == "P01"]
        r.record(
            "4",
            "our offer is open",
            len(ours) == 1 and ours[0].status is OfferStatus.OPEN,
        )
        r.expect(
            "4", "proposing transfers nothing", state.me.inventory, Bundle(30, 30, 30)
        )
        # Posting locks nothing server-side, so our own ledger must hold the stock.
        r.expect(
            "4",
            "two water are committed, not spent",
            self._commitments.available_to_commit(state),
            Bundle(28, 30, 30),
        )

    async def _step_5_observe_acceptance(self) -> None:
        state = await self._await_state(5)
        r = self._result
        r.expect("5", "world_version", state.world_version, 6)
        r.expect("5", "snapshot_sequence", state.snapshot_sequence, 5)

        ours = [o for o in state.offers if o.proposer_id == "P01"]
        r.record(
            "5",
            "our offer was accepted",
            len(ours) == 1 and ours[0].status is OfferStatus.ACCEPTED,
        )
        r.expect("5", "one transaction recorded", len(state.transactions), 1)
        r.expect("5", "inventory after the trade", state.me.inventory, Bundle(28, 31, 30))
        r.record(
            "5",
            "a settled offer no longer commits stock",
            self._commitments.available_to_commit(state) == Bundle(28, 31, 30),
        )

    async def _step_6_observe_gift(self) -> str:
        state = await self._await_state(6)
        r = self._result
        r.expect("6", "world_version", state.world_version, 7)
        r.expect("6", "snapshot_sequence", state.snapshot_sequence, 6)

        gifts = [o for o in state.incoming_open_offers() if o.is_gift_to("P01")]
        r.record(
            "6",
            "P02 offered a gift of one component",
            len(gifts) == 1
            and gifts[0].give == Bundle(components=1)
            and gifts[0].receive.is_zero(),
        )
        r.expect(
            "6", "a gift moves nothing until accepted", state.me.inventory, Bundle(28, 31, 30)
        )
        return gifts[0].offer_id if gifts else ""

    async def _step_7_accept_gift(self, offer_id: str) -> None:
        outcome = await self._executor.execute(AcceptAction(offer_id), ACCEPT_1, step="7")
        self._sent += 1

        r = self._result
        r.record("7", "accept succeeded", outcome.ok, outcome.code_name)
        r.record(
            "7",
            "a successful accept creates a transaction",
            outcome.result is not None and outcome.result.transaction_id is not None,
        )

        state = await self._await_state(7)
        self._executor.confirm(state)
        r.expect("7", "world_version", state.world_version, 8)
        r.expect("7", "snapshot_sequence", state.snapshot_sequence, 7)
        r.expect("7", "two transactions recorded", len(state.transactions), 2)
        r.expect("7", "inventory after the gift", state.me.inventory, Bundle(28, 31, 31))
        r.record(
            "7",
            "receiving a gift does not clear our advertisement",
            state.own_advertisement() is not None,
        )

    async def _step_8_withdraw(self, advertisement_id: str) -> None:
        outcome = await self._executor.execute(
            WithdrawAction(advertisement_id), WITHDRAW_1, step="8"
        )
        self._sent += 1

        r = self._result
        r.record("8", "withdraw succeeded", outcome.ok, outcome.code_name)

        state = await self._await_state(8)
        self._executor.confirm(state)
        r.expect("8", "world_version", state.world_version, 9)
        r.expect("8", "snapshot_sequence", state.snapshot_sequence, 8)
        r.record("8", "our advertisement is gone", state.own_advertisement() is None)
        r.expect("8", "completed trades remain", len(state.transactions), 2)
        r.expect("8", "inventory unchanged", state.me.inventory, Bundle(28, 31, 31))

    async def _step_9_request_limit(self) -> None:
        """The sixth command exceeds the stored-result limit on purpose."""
        action = AdvertiseAction(
            frozenset({Resource.WATER}), frozenset({Resource.FOOD}), TTL
        )
        outcome = await self._executor.execute(action, ADVERTISE_2, step="9")
        self._sent += 1

        r = self._result
        r.record(
            "9",
            "answered by a protocol_error rather than a result",
            outcome.result is None and outcome.error is not None,
            outcome.code_name,
        )
        if outcome.error is not None:
            r.expect(
                "9", "control code", outcome.error.code, ControlCode.REQUEST_CAPACITY_EXCEEDED
            )
            r.expect("9", "session stays open", outcome.error.close_session, False)
            r.expect("9", "error echoes our request id", outcome.error.request_id, ADVERTISE_2)
        self._executor.confirm(None)

    async def _step_10_sync(self) -> None:
        await self._executor.sync(step="10")
        self._sent += 1

        state = await self._await_state(9)
        self._executor.confirm(state)

        r = self._result
        r.expect("10", "snapshot_sequence advances", state.snapshot_sequence, 9)
        r.expect("10", "world_version does not", state.world_version, 9)
        r.expect("10", "final inventory", state.me.inventory, EXPECTED_FINAL_INVENTORY)
        r.expect("10", "two transactions", len(state.transactions), 2)
        r.expect("10", "five stored command results", len(state.request_results), 5)
        r.expect("10", "imported total", state.me.imported_total, Bundle(0, 1, 1))
        r.expect("10", "exported total", state.me.exported_total, Bundle(2, 0, 0))
        r.record(
            "10",
            "the rejected command created no advertisement",
            state.own_advertisement() is None,
        )

        self._result.final_inventory = state.me.inventory
        self._result.transaction_count = len(state.transactions)
        self._result.stored_results = len(state.request_results)


async def run_walkthrough(
    config: ClientConfig, evidence_path: Path | None = None
) -> WalkthroughResult:
    async with BazaarSession(config) as session:
        return await ScriptedWalkthrough(session, evidence_path).run()
