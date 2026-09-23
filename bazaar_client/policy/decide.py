"""One decision, given one observation.

A pure function of a snapshot plus carried memory: no socket, no protobuf, no
clock. That keeps the trading policy testable on synthetic states and makes the
reasoning behind an action inspectable after the fact.

The policy in one paragraph: we produce one resource and must import the other
two every tick. We keep enough of each import to outlast partners going quiet
(up to 60 ticks of upkeep, or the rest of the run) and pay for it only with
our own specialty, always one-for-one, in trades large enough to cover many
ticks at once, offered to the partners most likely to hold what we want. We
advertise exactly that. Once our imports are secure, spare specialty goes as
gifts to planets that keep asking for it, because one planet failing fails the
whole class.

Actions are chosen in order of what they protect, spending the tick's command
budget top down:

  1. accept offers worth taking  -- goods in, with no waiting on anyone
  2. withdraw offers turned dangerous -- stop promising what we now need
  3. refresh the advertisement   -- how counterparties find us at all
  4. propose targeted offers     -- fill our import buffers
  5. gift to a struggling planet -- only when our imports are secure
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bazaar_client.domain.types import Bundle, Phase, Resource, Snapshot
from bazaar_client.execution.actions import (
    AcceptAction,
    Action,
    OfferAction,
    WithdrawAction,
)
from bazaar_client.policy.accept import evaluate_incoming
from bazaar_client.policy.advertising import decide_advertisement
from bazaar_client.policy.altruism import scan_for_distress
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.pricing import TRADE_SIZE_MAX, CannotAfford, compute_terms
from bazaar_client.policy.reserves import (
    Urgency,
    compute_reserve,
    compute_urgency,
    deficit_below_reserve,
    import_target,
    specialty_spendable,
    surplus_above_reserve,
)
from bazaar_client.policy.targeting import rank_counterparties
from bazaar_client.world.commitments import CommitmentTracker

logger = logging.getLogger(__name__)

OFFER_TTL = 5
# Specialty kept back from gifts: enough to fund a full-size trade for each of
# our two imports.
GIFT_FLOOR = 2 * TRADE_SIZE_MAX


@dataclass
class Decision:
    """Chosen actions plus the reasoning, so a log can explain them."""

    actions: list[Action] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    reserve: Bundle = field(default_factory=Bundle.zero)
    available: Bundle = field(default_factory=Bundle.zero)
    surplus: Bundle = field(default_factory=Bundle.zero)
    deficit: Bundle = field(default_factory=Bundle.zero)
    targets: Bundle = field(default_factory=Bundle.zero)
    spendable: int = 0
    urgency: dict[Resource, Urgency] = field(default_factory=dict)

    def add(self, action: Action, reason: str) -> None:
        self.actions.append(action)
        self.reasons.append(reason)


def decide(
    observation: Snapshot,
    memory: PolicyMemory,
    commitments: CommitmentTracker | None = None,
    *,
    command_budget: int | None = None,
) -> tuple[Decision, PolicyMemory]:
    memory = memory.observe(observation)
    commitments = commitments or CommitmentTracker()
    specialty = observation.me.specialty

    reserve = compute_reserve(observation.rules, observation.me, memory)
    available = commitments.available_to_commit(observation)
    urgency = compute_urgency(available, observation.me.upkeep_per_tick, reserve)

    decision = Decision(
        reserve=reserve,
        available=available,
        surplus=surplus_above_reserve(available, reserve),
        deficit=deficit_below_reserve(available, reserve),
        targets=import_target(
            observation.me, reserve, observation.rules.duration_ticks - observation.tick
        ),
        spendable=specialty_spendable(available, reserve, specialty),
        urgency=urgency,
    )

    if observation.phase is not Phase.RUNNING:
        decision.reasons.append(f"phase is {observation.phase.name}; no trading actions")
        return decision, memory

    if observation.me.failed_once:
        decision.reasons.append("station has failed permanently; trading is disabled")
        return decision, memory

    if observation.tick < memory.blocked_until_tick:
        decision.reasons.append(
            f"rate limited until tick {memory.blocked_until_tick}"
        )
        return decision, memory

    if observation.tick >= observation.rules.duration_ticks:
        decision.reasons.append("run duration reached; no trading actions")
        return decision, memory

    budget = observation.rules.new_commands_per_station_per_tick
    if command_budget is not None:
        budget = min(budget, max(0, command_budget))

    # Spend from one shared balance throughout the proposed batch. Accepted
    # gains are not spendable until the server confirms them, but they do count
    # as on order so we do not ask elsewhere for the same units.
    budget, available, incoming = _accept_incoming(decision, observation, available, reserve, budget)
    urgency = compute_urgency(available, observation.me.upkeep_per_tick, reserve)
    budget = _withdraw_dangerous(decision, observation, urgency, budget)
    budget = _refresh_advertisement(decision, observation, available, reserve, budget)
    budget, available = _propose_offers(
        decision, observation, memory, available, reserve, incoming, budget
    )
    _offer_aid(decision, observation, memory, available, reserve, budget)

    return decision, memory


def _accept_incoming(decision, observation, available, reserve, budget):
    """Simulated locally so several accepts in one tick cannot overspend."""
    specialty = observation.me.specialty
    me = observation.self_station_id
    running = available
    incoming = Bundle.zero()
    # Gifts and the largest deliveries first: each accept settles immediately.
    offers = sorted(
        observation.incoming_open_offers(),
        key=lambda o: (-o.what_station_receives(me).total(), o.offer_id),
    )
    for offer in offers:
        if budget <= 0:
            break
        verdict = evaluate_incoming(
            offer, me, specialty, specialty_spendable(running, reserve, specialty)
        )
        if not verdict.accept:
            continue
        decision.add(AcceptAction(offer.offer_id), f"accept {offer.offer_id}: {verdict.reason}")
        budget -= 1
        running = running.saturating_sub(offer.what_station_pays(me))
        incoming = incoming + offer.what_station_receives(me)
    return budget, running, incoming


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


def _refresh_advertisement(decision, observation, available, reserve, budget):
    if budget <= 0:
        return budget
    specialty = observation.me.specialty
    verdict = decide_advertisement(
        specialty,
        specialty_spendable(available, reserve, specialty),
        observation.own_advertisement(),
        observation.tick,
        observation.rules,
    )
    if verdict.action is None:
        return budget
    decision.add(verdict.action, f"advertisement: {verdict.reason}")
    return budget - 1


def _wanted_quantities(observation, available, targets, incoming) -> dict[Resource, int]:
    """How far each import is below target, net of what is already on order.

    "On order" is what our open offers ask for plus what we accepted this tick.
    Our specialty is never wanted: we produce it.
    """
    on_order = incoming
    for offer in observation.outgoing_open_offers():
        on_order = on_order + offer.receive
    wanted: dict[Resource, int] = {}
    for resource in Resource:
        if resource == observation.me.specialty:
            continue
        short = targets.get(resource) - available.get(resource) - on_order.get(resource)
        if short > 0:
            wanted[resource] = short
    return wanted


def _propose_offers(decision, observation, memory, available, reserve, incoming, budget):
    specialty = observation.me.specialty
    open_outgoing = observation.outgoing_open_offers()
    room = observation.rules.max_open_outgoing_offers - len(open_outgoing)
    if room <= 0:
        decision.reasons.append("outgoing offer limit reached")
        return budget, available

    wanted = _wanted_quantities(observation, available, decision.targets, incoming)
    if not wanted:
        return budget, available

    # An offer already standing for this pairing still promises that stock.
    pending = frozenset(
        (offer.recipient_id, resource)
        for offer in open_outgoing
        for resource in Resource
        if offer.receive.get(resource) > 0
    )

    for candidate in rank_counterparties(
        memory.counterparties, specialty, wanted, observation.tick, already_pending=pending
    ):
        if budget <= 0 or room <= 0:
            break
        try:
            give, receive = compute_terms(
                candidate.want,
                min(wanted[candidate.want], candidate.size_limit),
                specialty,
                specialty_spendable(available, reserve, specialty),
            )
        except CannotAfford:
            continue

        ttl = min(OFFER_TTL, observation.rules.max_offer_ttl_ticks,
                  observation.rules.duration_ticks - observation.tick)
        if ttl <= 0:
            break
        decision.add(
            OfferAction(candidate.station_id, give, receive, observation.tick + ttl),
            f"offer {give.total()} {specialty.name} for {receive.total()} "
            f"{candidate.want.name} to {candidate.station_id}",
        )
        available = available.saturating_sub(give)
        budget -= 1
        room -= 1
    return budget, available


def _offer_aid(decision, observation, memory, available, reserve, budget):
    planned_offers = sum(isinstance(a, OfferAction) for a in decision.actions)
    if (budget <= 0 or len(observation.outgoing_open_offers()) + planned_offers
            >= observation.rules.max_open_outgoing_offers):
        return
    specialty = observation.me.specialty
    targets = decision.targets
    if any(available.get(r) < targets.get(r) for r in Resource if r != specialty):
        return  # secure our own imports first

    floor = max(reserve.get(specialty), GIFT_FLOOR * observation.me.upkeep_per_tick.get(specialty))
    excess = available.get(specialty) - floor
    for gift in scan_for_distress(
        memory.counterparties, specialty, excess, observation.tick, memory
    ):
        if budget <= 0:
            break
        ttl = min(OFFER_TTL, observation.rules.max_offer_ttl_ticks,
                  observation.rules.duration_ticks - observation.tick)
        if ttl <= 0:
            break
        decision.add(
            OfferAction(
                gift.station_id,
                Bundle.single(gift.resource, gift.quantity),
                Bundle.zero(),
                observation.tick + ttl,
            ),
            f"gift {gift.quantity} {gift.resource.name} to {gift.station_id}: "
            "keeps seeking what we produce",
        )
        memory.record_gift(gift.station_id, gift.resource, observation.tick)
        budget -= 1
