"""One decision, given one observation.

A pure function of a snapshot plus carried memory: no socket, no protobuf, no
clock. That keeps the trading policy testable on synthetic states and makes the
reasoning behind an action inspectable after the fact.

Actions are chosen in order of what they protect, spending the tick's command
budget top down:

  1. accept offers worth taking  -- the only action that actually moves goods in
  2. withdraw offers turned dangerous -- stop promising what we now need
  3. refresh the advertisement   -- how counterparties find us at all
  4. propose targeted offers     -- fill what we are short of
  5. gift to a struggling planet -- only when nothing of ours is at risk
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bazaar_client.domain.types import Bundle, Phase, Resource, Snapshot
from bazaar_client.execution.actions import (
    AcceptAction,
    Action,
    AdvertiseAction,
    OfferAction,
    WithdrawAction,
)
from bazaar_client.policy.accept import evaluate_incoming
from bazaar_client.policy.advertising import decide_advertisement
from bazaar_client.policy.altruism import scan_for_distress
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.pricing import CannotAfford, compute_terms, desired_quantity
from bazaar_client.policy.reserves import (
    Urgency,
    compute_reserve,
    compute_urgency,
    deficit_below_reserve,
    surplus_above_reserve,
)
from bazaar_client.policy.targeting import rank_counterparties
from bazaar_client.world.commitments import CommitmentTracker

logger = logging.getLogger(__name__)

OFFER_TTL = 5
# Stock we cannot produce is worth holding deeper than the bare reserve, since
# replacing it depends entirely on someone else agreeing to trade.
TARGET_BUFFER_MULTIPLE = 2


@dataclass
class Decision:
    """Chosen actions plus the reasoning, so a log can explain them."""

    actions: list[Action] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    reserve: Bundle = field(default_factory=Bundle.zero)
    available: Bundle = field(default_factory=Bundle.zero)
    surplus: Bundle = field(default_factory=Bundle.zero)
    deficit: Bundle = field(default_factory=Bundle.zero)
    urgency: dict[Resource, Urgency] = field(default_factory=dict)

    def add(self, action: Action, reason: str) -> None:
        self.actions.append(action)
        self.reasons.append(reason)


def decide(
    observation: Snapshot,
    memory: PolicyMemory,
    commitments: CommitmentTracker | None = None,
) -> tuple[Decision, PolicyMemory]:
    memory = memory.observe(observation)
    commitments = commitments or CommitmentTracker()

    reserve = compute_reserve(observation.rules, observation.me, memory)
    available = commitments.available_to_commit(observation)
    urgency = compute_urgency(available, observation.me.upkeep_per_tick, reserve)
    surplus = surplus_above_reserve(available, reserve)
    deficit = deficit_below_reserve(available, reserve)
    critical = frozenset(r for r, level in urgency.items() if level is Urgency.CRITICAL)

    decision = Decision(
        reserve=reserve,
        available=available,
        surplus=surplus,
        deficit=deficit,
        urgency=urgency,
    )

    if observation.phase is not Phase.RUNNING:
        decision.reasons.append(f"phase is {observation.phase.name}; no trading actions")
        return decision, memory

    if observation.me.failed_once and observation.me.health == 0:
        decision.reasons.append("station has failed permanently; trading is disabled")
        return decision, memory

    if observation.tick < memory.blocked_until_tick:
        decision.reasons.append(
            f"rate limited until tick {memory.blocked_until_tick}"
        )
        return decision, memory

    budget = observation.rules.new_commands_per_station_per_tick

    budget = _accept_incoming(decision, observation, available, reserve, urgency, budget)
    budget = _withdraw_dangerous(decision, observation, urgency, budget)
    budget = _refresh_advertisement(
        decision, observation, surplus, deficit, critical, budget
    )
    budget = _propose_offers(
        decision, observation, memory, surplus, deficit, urgency, budget
    )
    _offer_aid(decision, observation, memory, surplus, urgency, budget)

    return decision, memory


def _accept_incoming(decision, observation, available, reserve, urgency, budget):
    # Simulated locally so several accepts in one tick cannot overspend.
    running = available
    for offer in observation.incoming_open_offers():
        if budget <= 0:
            break
        verdict = evaluate_incoming(
            offer, observation.self_station_id, running, reserve, urgency
        )
        if not verdict.accept:
            continue
        decision.add(AcceptAction(offer.offer_id), f"accept {offer.offer_id}: {verdict.reason}")
        budget -= 1
        running = running.saturating_sub(
            offer.what_station_pays(observation.self_station_id)
        ) + offer.what_station_receives(observation.self_station_id)
    return budget


def _withdraw_dangerous(decision, observation, urgency, budget):
    """Expiry is free, so only spend a command when honouring terms would hurt."""
    for offer in observation.outgoing_open_offers():
        if budget <= 0:
            break
        at_risk = [
            r for r in Resource if offer.give.get(r) > 0 and urgency[r] is Urgency.CRITICAL
        ]
        if not at_risk:
            continue
        decision.add(
            WithdrawAction(offer.offer_id),
            f"withdraw {offer.offer_id}: promises {at_risk[0].name} we now need",
        )
        budget -= 1
    return budget


def _refresh_advertisement(decision, observation, surplus, deficit, critical, budget):
    if budget <= 0:
        return budget
    verdict = decide_advertisement(
        surplus,
        deficit,
        critical,
        observation.own_advertisement(),
        observation.tick,
        observation.rules,
    )
    if verdict.action is None:
        return budget
    decision.add(verdict.action, f"advertisement: {verdict.reason}")
    return budget - 1


def _wanted_quantities(observation, available, reserve, deficit) -> dict[Resource, int]:
    """What to ask for, including before a shortage actually bites.

    Our specialty is the only resource we generate; the other two can only ever
    arrive by trade. Waiting for a deficit before acting means starting the
    exchange with no buffer left, so surplus is converted toward a target of
    twice the reserve while there is still something to trade with.
    """
    wanted: dict[Resource, int] = {}
    for resource in Resource:
        short = deficit.get(resource)
        if resource != observation.me.specialty:
            target = reserve.get(resource) * TARGET_BUFFER_MULTIPLE
            short = max(short, target - available.get(resource))
        if short > 0:
            wanted[resource] = short
    return wanted


def _propose_offers(decision, observation, memory, surplus, deficit, urgency, budget):
    open_outgoing = observation.outgoing_open_offers()
    room = observation.rules.max_open_outgoing_offers - len(open_outgoing)
    if room <= 0:
        decision.reasons.append("outgoing offer limit reached")
        return budget

    # An offer already standing for this pairing still promises that stock.
    pending = frozenset(
        (offer.recipient_id, resource)
        for offer in open_outgoing
        for resource in Resource
        if offer.receive.get(resource) > 0
    )

    wanted = _wanted_quantities(
        observation, decision.available, decision.reserve, deficit
    )

    running_surplus = surplus
    for candidate in rank_counterparties(
        memory.counterparties,
        urgency,
        wanted,
        running_surplus,
        observation.tick,
        already_pending=pending,
    ):
        if budget <= 0 or room <= 0:
            break
        try:
            give, receive = compute_terms(
                candidate.want,
                desired_quantity(wanted[candidate.want]),
                candidate.give,
                running_surplus,
                urgency[candidate.want],
            )
        except CannotAfford:
            continue

        ttl = min(OFFER_TTL, observation.rules.max_offer_ttl_ticks)
        decision.add(
            OfferAction(candidate.station_id, give, receive, observation.tick + ttl),
            f"offer {give.total()} {candidate.give.name} for {receive.total()} "
            f"{candidate.want.name} to {candidate.station_id}",
        )
        running_surplus = running_surplus.saturating_sub(give)
        budget -= 1
        room -= 1
    return budget


def _offer_aid(decision, observation, memory, surplus, urgency, budget):
    if budget <= 0:
        return
    for gift in scan_for_distress(
        memory.counterparties, surplus, urgency, observation.tick, memory
    ):
        if budget <= 0:
            break
        ttl = min(OFFER_TTL, observation.rules.max_offer_ttl_ticks)
        decision.add(
            OfferAction(
                gift.station_id,
                Bundle.single(gift.resource, gift.quantity),
                Bundle.zero(),
                observation.tick + ttl,
            ),
            f"gift {gift.quantity} {gift.resource.name} to {gift.station_id}: "
            "sustained unmet need",
        )
        memory.record_gift(gift.station_id, gift.resource, observation.tick)
        budget -= 1
