"""Turns chosen actions into commands, sends them, and records what happened.

A successful advertise means the claim was published and a successful offer
means the proposal exists -- neither means a trade occurred. Only a successful
accept settles an exchange, so outcomes are recorded per action rather than
reported as "command sent".
"""

from __future__ import annotations

import logging

from bazaar_client.app import BazaarSession, CommandOutcome
from bazaar_client.domain import mappers
from bazaar_client.domain.types import ResultCode
from bazaar_client.execution.actions import (
    AcceptAction,
    Action,
    AdvertiseAction,
    OfferAction,
    SyncAction,
    WithdrawAction,
)
from bazaar_client.execution.evidence import EvidenceLog
from bazaar_client.world.commitments import CommitmentTracker
from bazaar_client.world.counterparties import CounterpartyModel

logger = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        session: BazaarSession,
        evidence: EvidenceLog,
        commitments: CommitmentTracker | None = None,
        counterparties: CounterpartyModel | None = None,
    ) -> None:
        self._session = session
        self._evidence = evidence
        self._commitments = commitments or CommitmentTracker()
        self._counterparties = counterparties or CounterpartyModel()
        self._last_record = None

    @property
    def commitments(self) -> CommitmentTracker:
        return self._commitments

    def build(self, action: Action, run_id: str, request_id: str):
        if isinstance(action, AdvertiseAction):
            return mappers.build_advertise(
                run_id, request_id, action.selling, action.seeking, action.expires_tick
            )
        if isinstance(action, OfferAction):
            return mappers.build_offer(
                run_id,
                request_id,
                action.recipient_id,
                action.give,
                action.receive,
                action.expires_tick,
            )
        if isinstance(action, AcceptAction):
            return mappers.build_accept(run_id, request_id, action.offer_id)
        if isinstance(action, WithdrawAction):
            return mappers.build_withdraw(run_id, request_id, action.object_id)
        raise TypeError(f"{type(action).__name__} is not a command action")

    async def execute(
        self, action: Action, request_id: str, step: str = "", timeout: float = 15.0
    ) -> CommandOutcome:
        """Send one command and record its outcome against the state it came from."""
        observed = self._session.latest_snapshot
        record = self._evidence.start(step or action.kind, action, observed)
        record.request_id = request_id
        self._last_record = record

        message = self.build(action, self._session.run_id, request_id)

        if isinstance(action, OfferAction):
            # No reservation exists server-side, so hold the stock ourselves
            # until this offer shows up in a snapshot.
            self._commitments.register_inflight(request_id, action.give)

        try:
            outcome = await self._session.send_command(
                message, kind=action.kind, request_id=request_id, timeout=timeout
            )
        except BaseException:
            # A timeout or lost connection must not leave stock reserved for an
            # offer we never learned the fate of, nor drop the evidence.
            self._evidence.note(record, "no answer: send failed or connection lost")
            self._evidence.complete(record)
            raise
        finally:
            self._commitments.resolve_inflight(request_id)

        self._record_outcome(record, outcome)
        return outcome

    def _record_outcome(self, record, outcome: CommandOutcome) -> None:
        if outcome.result is not None:
            record.result_ok = outcome.result.ok
            record.result_code = outcome.result.code.name
            record.object_id = outcome.result.object_id
            record.transaction_id = outcome.result.transaction_id
            if not outcome.result.ok:
                logger.warning(
                    "%s was rejected: %s", record.action_kind, outcome.result.code.name
                )
            if outcome.result.code is ResultCode.STATION_FAILED:
                recipient = record.action.get("recipient_id")
                if recipient:
                    self._counterparties.mark_failed(recipient)
        elif outcome.error is not None:
            record.result_ok = False
            record.result_code = outcome.error.code.name
            self._evidence.note(record, "answered by protocol_error, not a result")

    async def sync(self, step: str = "sync") -> None:
        """Ask for a fresh snapshot. No request id, so no result comes back."""
        action = SyncAction()
        record = self._evidence.start(step, action, self._session.latest_snapshot)
        self._last_record = record
        await self._session.send_sync()
        self._evidence.note(record, "control message; answered by a state only")

    def confirm(self, snapshot=None) -> None:
        """Close the most recent record once its follow-up state has arrived."""
        if self._last_record is not None:
            self._evidence.complete(self._last_record, snapshot)
            self._last_record = None
