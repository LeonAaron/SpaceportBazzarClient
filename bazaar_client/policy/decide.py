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
from bazaar_client.policy.altruism import (
    MAX_EXCESS_GIFT, pick_gift_recipient, scan_for_distress,
)
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.pricing import (
    MAX_BULK_TRADE_SIZE, MAX_TRADE_SIZE, CannotAfford, compute_terms, desired_quantity,
    trade_size,
)
from bazaar_client.policy.reserves import (
    Urgency,
    WATCH_STOCK,
    PRODUCTION_NORMAL_TICKS,
    PRODUCTION_STOP_TICKS,
    compute_reserve,
    comfortably_supplied,
    import_ceiling,
    in_end_game,
    over_ceiling,
    production_payment_limit,
    compute_urgency,
    deficit_below_reserve,
    excess_over_cap,
    scarcest_import,
    specialty_floor,
    STOCK_CAP,
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
    *,
    command_budget: int | None = None,
) -> tuple[Decision, PolicyMemory]:
    memory = memory.observe(observation)
    commitments = commitments or CommitmentTracker()

    available = commitments.available_to_commit(observation)
    reserve = compute_reserve(observation.rules, observation.me, memory, available)
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

    ticks_left = observation.rules.duration_ticks - observation.tick
    hold_for = _scarce_import_to_chase(observation, available, urgency, ticks_left)
    budget, available, deferred = _accept_incoming(
        decision, observation, observation.incoming_open_offers(),
        available, reserve, urgency, budget, ticks_left, hold_for=hold_for,
    )
    # Spend from one shared balance throughout the proposed batch. Accepted
    # gains are not spendable until confirmed by the server.
    surplus = surplus_above_reserve(available, reserve)
    deficit = deficit_below_reserve(available, reserve)
    urgency = compute_urgency(available, observation.me.upkeep_per_tick, reserve)
    critical = frozenset(r for r, level in urgency.items() if level is Urgency.CRITICAL)
    budget = _withdraw_dangerous(decision, observation, available, budget, ticks_left)
    excess = excess_over_cap(available)
    budget, available = _give_excess(decision, observation, memory, available, excess, budget)
    surplus = surplus_above_reserve(available, reserve)
    wanted = _wanted_quantities(observation, available, reserve, deficit)
    # Never ask for what we are giving away; offer it to whoever needs it.
    advertised_need = Bundle(*(
        wanted.get(r, 0) if excess.get(r) == 0 else 0 for r in Resource
    ))
    advertised_surplus = Bundle(*(
        surplus.get(r) if r not in wanted or excess.get(r) > 0 else 0 for r in Resource
    ))
    if production_payment_limit(available, observation.me, ticks_left) == 0:
        advertised_surplus = Bundle.zero()
    if comfortably_supplied(available):
        current_ad = observation.own_advertisement()
        if current_ad is not None and budget > 0:
            decision.add(WithdrawAction(current_ad.advertisement_id),
                         "withdraw advertisement: all resources above 30")
            budget -= 1
    else:
        budget = _refresh_advertisement(
            decision, observation, advertised_surplus, advertised_need, critical, budget
        )
    budget, surplus, available = _propose_offers(
        decision, observation, memory, available, surplus, deficit, urgency, budget,
        ticks_left,
    )
    if deferred and budget > 0:
        # Nothing better used the command, so take what was held back.
        urgency = compute_urgency(available, observation.me.upkeep_per_tick, reserve)
        budget, available, _ = _accept_incoming(
            decision, observation, deferred, available, reserve, urgency, budget,
            ticks_left,
        )
        surplus = surplus_above_reserve(available, reserve)
    _offer_aid(decision, observation, memory, surplus, urgency, budget, ticks_left)

    return decision, memory


def _scarce_import_to_chase(observation, available, urgency, ticks_left):
    """The import worth keeping a command free for, if accepts would starve it.

    With one command a tick, accepting whatever arrives first can spend every
    tick buying the import we already have plenty of while the other drains.
    """
    specialty = observation.me.specialty
    scarce = scarcest_import(available, specialty)
    if scarce is None:
        return None
    lagging = urgency[scarce] is not Urgency.NONE or any(
        over_ceiling(r, available, specialty)
        for r in Resource if r not in (scarce, specialty)
    )
    if not lagging:
        return None
    if production_payment_limit(available, observation.me, ticks_left) == 0:
        return None  # we could not pay for an offer anyway
    outgoing = observation.outgoing_open_offers()
    if len(outgoing) >= observation.rules.max_open_outgoing_offers:
        return None
    if any(o.receive.get(scarce) > 0 for o in outgoing):
        return None  # already asking for it; let accepts run
    return scarce


def _accept_incoming(decision, observation, offers, available, reserve, urgency, budget,
                     ticks_left=None, *, hold_for=None):
    """Accept what is worth taking; return (budget, available, deferred offers).

    Offers in their last usable tick, and, while `hold_for` is set, offers that
    do not bring it, are deferred rather than spending the tick's last command.
    """
    # Simulated locally so several accepts in one tick cannot overspend.
    running = available
    deferred = []
    end_game = in_end_game(ticks_left)
    preserve_normal = _has_normal_offers(observation)
    for offer in offers:
        if budget <= 0:
            break
        cost = offer.what_station_pays(observation.self_station_id)
        gain = offer.what_station_receives(observation.self_station_id)
        after_payment = running.saturating_sub(cost)
        limit = production_payment_limit(after_payment, observation.me, ticks_left)
        if not cost.is_zero() and preserve_normal and limit is not None:
            continue
        verdict = evaluate_incoming(
            offer, observation.self_station_id, running, reserve, urgency,
            specialty=observation.me.specialty,
            max_payment_ratio=limit,
            end_game=end_game,
        )
        if not verdict.accept:
            continue
        last_chance = offer.expires_tick - observation.tick <= 1
        if budget <= 1 and (last_chance or (hold_for is not None and gain.get(hold_for) == 0)):
            deferred.append(offer)
            continue
        decision.add(AcceptAction(offer.offer_id), f"accept {offer.offer_id}: {verdict.reason}")
        budget -= 1
        running = running.saturating_sub(cost)
        urgency = compute_urgency(running, observation.me.upkeep_per_tick, reserve)
    return budget, running, deferred


def _has_normal_offers(observation):
    return any(o.receive.is_zero() or o.give.total() > 0.5 * o.receive.total()
               for o in observation.outgoing_open_offers())


def _withdraw_dangerous(decision, observation, available, budget, ticks_left=None):
    """Expiry is free, so only spend a command when honouring terms would hurt."""
    for offer in observation.outgoing_open_offers():
        if budget <= 0:
            break
        at_risk = [
            r for r in Resource if offer.give.get(r) > 0
            and available.get(r) < decision.reserve.get(r)
        ]
        limit = production_payment_limit(available, observation.me, ticks_left)
        violates_guard = limit is not None and (
            offer.receive.is_zero() or limit == 0 or offer.give.total() > limit * offer.receive.total()
        )
        if not at_risk and not violates_guard:
            continue
        decision.add(
            WithdrawAction(offer.offer_id),
            f"withdraw {offer.offer_id}: " + (
                "production supply guard" if violates_guard
                else f"promises {at_risk[0].name} we now need"
            ),
        )
        budget -= 1
    return budget


def _give_excess(decision, observation, memory, available, excess, budget):
    """Gift stock above STOCK_CAP to planets advertising that they need it.

    Runs before new offers: past the cap, the units are worth more to a planet
    that is asking than to us, and a step left until last rarely gets a command.
    """
    room = (observation.rules.max_open_outgoing_offers
            - len(observation.outgoing_open_offers())
            - sum(isinstance(a, OfferAction) for a in decision.actions))
    ttl = min(OFFER_TTL, observation.rules.max_offer_ttl_ticks,
              observation.rules.duration_ticks - observation.tick)
    for resource in sorted(Resource, key=lambda r: -excess.get(r)):
        if budget <= 0 or room <= 0 or ttl <= 0:
            break
        if excess.get(resource) <= 0:
            break
        recipient = pick_gift_recipient(
            memory.counterparties, resource, observation.tick, memory
        )
        if recipient is None:
            continue
        quantity = min(excess.get(resource), MAX_EXCESS_GIFT)
        gift = Bundle.single(resource, quantity)
        decision.add(
            OfferAction(recipient, gift, Bundle.zero(), observation.tick + ttl),
            f"gift {quantity} {resource.name} to {recipient}: over {STOCK_CAP} cap, "
            f"{recipient} seeking {resource.name}",
        )
        memory.record_gift(recipient, resource, observation.tick)
        available = available.saturating_sub(gift)
        budget -= 1
        room -= 1
    return budget, available


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
    exchange with no buffer left. Keep seeking small trades even above the
    20-unit buffer, using our renewable specialty to cover future consumption.

    An import past its balance ceiling only counts a real deficit. While spare
    production allows bulk lots, imports are wanted up to the ceiling, so idle
    production keeps buying both while neither runs ahead. The ceiling never
    passes STOCK_CAP: past it we give stock away, so asking for more is churn.
    """
    specialty = observation.me.specialty
    spare = available.get(specialty) - specialty_floor(observation.me)
    bulk = trade_size(spare) > MAX_TRADE_SIZE
    wanted: dict[Resource, int] = {}
    for resource in Resource:
        short = deficit.get(resource)
        if resource != specialty:
            have = available.get(resource)
            ceiling = min(import_ceiling(resource, available, specialty) or STOCK_CAP, STOCK_CAP)
            if have >= STOCK_CAP or have > ceiling:
                if short > 0:
                    wanted[resource] = short
                continue
            target = max(WATCH_STOCK, reserve.get(resource) * TARGET_BUFFER_MULTIPLE)
            room = ceiling - have if bulk else 0
            short = min(max(short, target - have, 5, room), STOCK_CAP - have)
        if short > 0:
            wanted[resource] = short
    return wanted


def _propose_offers(decision, observation, memory, available, surplus, deficit, urgency, budget,
                    ticks_left=None):
    open_outgoing = observation.outgoing_open_offers()
    room = observation.rules.max_open_outgoing_offers - len(open_outgoing)
    if room <= 0:
        decision.reasons.append("outgoing offer limit reached")
        return budget, surplus, available

    # An offer already standing for this pairing still promises that stock.
    pending = frozenset(
        (offer.recipient_id, resource)
        for offer in open_outgoing
        for resource in Resource
        if offer.receive.get(resource) > 0
    )

    wanted = _wanted_quantities(
        observation, available, decision.reserve, deficit
    )

    running_surplus = surplus
    running_available = available
    preserve_normal = _has_normal_offers(observation)
    specialty = observation.me.specialty
    end_game = in_end_game(ticks_left)
    for candidate in rank_counterparties(
        memory.counterparties,
        urgency,
        wanted,
        running_surplus,
        observation.tick,
        already_pending=pending,
        preferred_give=specialty,
    ):
        if budget <= 0 or room <= 0:
            break
        # Price from the balance AFTER reserving payment. Normal offers must
        # leave 25 ticks; half-price offers must leave 10. Existing normal
        # offers keep the higher floor so a new offer cannot invalidate them.
        # In the end game only the upkeep still to come is kept back.
        terms = None
        for limit in ((None,) if end_game else (None, 0.5)):
            if end_game:
                floor = specialty_floor(observation.me, ticks_left)
            else:
                floor_ticks = (PRODUCTION_NORMAL_TICKS if limit is None or preserve_normal
                               else PRODUCTION_STOP_TICKS)
                floor = floor_ticks * observation.me.upkeep_per_tick.get(specialty)
            if running_available.get(specialty) < floor:
                continue
            payable = Bundle(*(
                min(running_surplus.get(r), running_available.get(r) - floor)
                if r == specialty else running_surplus.get(r) for r in Resource
            ))
            spare = running_available.get(specialty) - specialty_floor(observation.me, ticks_left)
            size = MAX_BULK_TRADE_SIZE if end_game else trade_size(spare)
            try:
                terms = compute_terms(
                    candidate.want,
                    desired_quantity(wanted[candidate.want],
                                     running_available.get(candidate.want), size),
                    candidate.give, payable, urgency[candidate.want],
                    max_payment_ratio=limit,
                    stock=running_available.get(candidate.want),
                    end_game=end_game and candidate.give == specialty,
                )
                break
            except CannotAfford:
                continue
        if terms is None:
            continue
        give, receive = terms
        preserve_normal |= give.total() > 0.5 * receive.total()

        ttl = min(OFFER_TTL, observation.rules.max_offer_ttl_ticks,
                  observation.rules.duration_ticks - observation.tick)
        if ttl <= 0:
            break
        decision.add(
            OfferAction(candidate.station_id, give, receive, observation.tick + ttl),
            f"offer {give.total()} {candidate.give.name} for {receive.total()} "
            f"{candidate.want.name} to {candidate.station_id}",
        )
        running_surplus = running_surplus.saturating_sub(give)
        running_available = running_available.saturating_sub(give)
        budget -= 1
        room -= 1
    return budget, running_surplus, running_available


def _offer_aid(decision, observation, memory, surplus, urgency, budget, ticks_left=None):
    if production_payment_limit(surplus + decision.reserve, observation.me, ticks_left) is not None:
        return
    planned_offers = sum(isinstance(a, OfferAction) for a in decision.actions)
    if (budget <= 0 or len(observation.outgoing_open_offers()) + planned_offers
            >= observation.rules.max_open_outgoing_offers):
        return
    for gift in scan_for_distress(
        memory.counterparties, surplus, urgency, observation.tick, memory
    ):
        if budget <= 0:
            break
        after_gift = (surplus + decision.reserve).saturating_sub(
            Bundle.single(gift.resource, gift.quantity)
        )
        if production_payment_limit(after_gift, observation.me, ticks_left) is not None:
            continue
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
            "sustained unmet need",
        )
        memory.record_gift(gift.station_id, gift.resource, observation.tick)
        budget -= 1
