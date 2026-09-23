"""The whole decision, composed.

`decide` is a pure function of a snapshot plus memory, so every situation here
is built by hand -- no socket, no server, no clock.
"""

from __future__ import annotations

from bazaar_client.domain.types import (
    Bundle,
    OfferStatus,
    Phase,
    Resource,
)
from bazaar_client.execution.actions import (
    AcceptAction,
    AdvertiseAction,
    OfferAction,
    WithdrawAction,
)
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.reserves import Urgency
from bazaar_client.world.commitments import CommitmentTracker
from tests.fixtures import factories


def snapshot(**kwargs):
    kwargs.setdefault("self_station_id", "P01")
    return factories.make_snapshot(**kwargs)


def station(**kwargs):
    return factories.make_station(**kwargs)


def peer_selling(resource, seeking=frozenset(), station_id="P02", tick=0):
    return factories.make_advertisement(
        station_id=station_id,
        selling=frozenset({resource}),
        seeking=seeking,
        created_tick=tick,
        expires_tick=tick + 50,
    )


def kinds(decision):
    return [type(a).__name__ for a in decision.actions]


def of_type(decision, cls):
    return [a for a in decision.actions if isinstance(a, cls)]


# --- gating ---------------------------------------------------------------


def test_no_trading_actions_outside_a_running_phase():
    """The instructor starts the run; readiness alone does not."""
    for phase in (Phase.READY, Phase.PAUSED, Phase.FINISHED, Phase.ABORTED):
        decision, _ = decide(snapshot(phase=phase), PolicyMemory())

        assert decision.actions == []
        assert phase.name in decision.reasons[0]


def test_no_actions_while_rate_limited():
    """A rate-limited station's next command of any kind is rejected anyway."""
    memory = PolicyMemory()
    memory.block_until(9)

    decision, _ = decide(snapshot(tick=5), memory)

    assert decision.actions == []
    assert "rate limited" in decision.reasons[0]


def test_trading_resumes_after_the_retry_tick():
    memory = PolicyMemory()
    memory.block_until(9)

    decision, _ = decide(snapshot(tick=9, me=station(inventory=Bundle(30, 30, 30))), memory)

    assert decision.actions


def test_a_permanently_failed_station_stops_trading():
    """Zero health is permanent; trading is disabled and gifts cannot rescue it."""
    decision, _ = decide(
        snapshot(me=station(health=0, failed_once=True)), PolicyMemory()
    )

    assert decision.actions == []
    assert "failed permanently" in decision.reasons[0]


# --- priority order -------------------------------------------------------


def test_accepting_comes_before_proposing():
    """Accepting is the only action that actually moves goods in."""
    gift = factories.make_offer(
        offer_id="gift", proposer_id="P02", recipient_id="P01",
        give=Bundle(food=2), receive=Bundle.zero(), expires_tick=50,
    )
    decision, _ = decide(
        snapshot(
            me=station(inventory=Bundle(30, 0, 30)),
            offers=(gift,),
            advertisements=(peer_selling(Resource.FOOD),),
        ),
        PolicyMemory(),
    )

    assert kinds(decision)[0] == "AcceptAction"


def test_the_command_budget_is_never_exceeded():
    """new_commands_per_station_per_tick is read from the rules, not assumed."""
    offers = tuple(
        factories.make_offer(
            offer_id=f"gift-{i}", proposer_id="P02", recipient_id="P01",
            give=Bundle(food=1), receive=Bundle.zero(), expires_tick=50,
        )
        for i in range(10)
    )
    decision, _ = decide(
        snapshot(
            rules=factories.make_rules(new_commands_per_station_per_tick=2),
            me=station(inventory=Bundle(30, 0, 30)),
            offers=offers,
        ),
        PolicyMemory(),
    )

    assert len(decision.actions) == 2


def test_every_action_carries_a_stated_reason():
    """Logs have to explain decisions, not just record them."""
    decision, _ = decide(
        snapshot(me=station(inventory=Bundle(30, 30, 30))), PolicyMemory()
    )

    assert len(decision.reasons) >= len(decision.actions)
    assert all(reason for reason in decision.reasons)


# --- accepting ------------------------------------------------------------


def test_a_gift_addressed_to_us_is_accepted():
    gift = factories.make_offer(
        offer_id="gift", proposer_id="P02", recipient_id="P01",
        give=Bundle(components=1), receive=Bundle.zero(), expires_tick=50,
    )
    decision, _ = decide(snapshot(offers=(gift,)), PolicyMemory())

    assert AcceptAction("gift") in decision.actions


def test_an_offer_we_proposed_is_never_accepted_by_us():
    """Only the recipient may accept."""
    mine = factories.make_offer(
        offer_id="mine", proposer_id="P01", recipient_id="P02",
        give=Bundle(water=1), receive=Bundle.zero(), expires_tick=50,
    )
    decision, _ = decide(snapshot(offers=(mine,)), PolicyMemory())

    assert of_type(decision, AcceptAction) == []


def test_an_expired_offer_is_not_accepted():
    """Deadlines are exclusive: at expires_tick it can no longer settle."""
    stale = factories.make_offer(
        offer_id="stale", proposer_id="P02", recipient_id="P01",
        give=Bundle(food=5), receive=Bundle.zero(), expires_tick=4,
    )
    decision, _ = decide(snapshot(tick=4, offers=(stale,)), PolicyMemory())

    assert of_type(decision, AcceptAction) == []


def test_several_accepts_in_one_tick_cannot_overspend_the_same_stock():
    """Settlement is atomic per offer, so each accept must be affordable in turn."""
    offers = tuple(
        factories.make_offer(
            offer_id=f"o{i}", proposer_id="P02", recipient_id="P01",
            give=Bundle(food=3), receive=Bundle(water=3), expires_tick=50,
        )
        for i in range(4)
    )
    decision, _ = decide(
        snapshot(
            rules=factories.make_rules(new_commands_per_station_per_tick=10),
            # Reserve is 3 water, so 5 are spendable: enough for one 3-for-3 accept.
            me=station(inventory=Bundle(water=8, food=0, components=30)),
            offers=offers,
        ),
        PolicyMemory(),
    )

    assert len(of_type(decision, AcceptAction)) == 1


# --- proposing ------------------------------------------------------------


def test_a_shortfall_produces_a_targeted_offer():
    decision, _ = decide(
        snapshot(
            me=station(inventory=Bundle(water=30, food=0, components=30)),
            advertisements=(peer_selling(Resource.FOOD, seeking=frozenset({Resource.WATER})),),
        ),
        PolicyMemory(),
    )

    offers = of_type(decision, OfferAction)
    assert offers
    assert offers[0].recipient_id == "P02"
    assert offers[0].receive.food > 0
    assert offers[0].give.water > 0


def test_offers_respect_the_open_offer_limit():
    """LIMIT_REACHED is avoidable by reading max_open_outgoing_offers."""
    open_offers = tuple(
        factories.make_offer(
            offer_id=f"mine-{i}", proposer_id="P01", recipient_id="P02",
            give=Bundle(water=1), receive=Bundle(food=1), expires_tick=50,
        )
        for i in range(2)
    )
    decision, _ = decide(
        snapshot(
            rules=factories.make_rules(max_open_outgoing_offers=2),
            me=station(inventory=Bundle(water=30, food=0, components=30)),
            offers=open_offers,
            advertisements=(peer_selling(Resource.FOOD),),
        ),
        PolicyMemory(),
    )

    assert of_type(decision, OfferAction) == []
    assert "outgoing offer limit reached" in decision.reasons


def test_offers_never_promise_stock_already_committed():
    """Posting reserves nothing server-side, so our ledger must hold the line."""
    committed = factories.make_offer(
        offer_id="mine", proposer_id="P01", recipient_id="P03",
        give=Bundle(water=29), receive=Bundle(components=1), expires_tick=50,
    )
    commitments = CommitmentTracker()
    state = snapshot(
        me=station(inventory=Bundle(water=30, food=0, components=30)),
        offers=(committed,),
        advertisements=(peer_selling(Resource.FOOD),),
    )

    decision, _ = decide(state, PolicyMemory(), commitments)

    for offer in of_type(decision, OfferAction):
        assert offer.give.water <= 1


def test_the_offer_ttl_respects_the_published_cap():
    decision, _ = decide(
        snapshot(
            tick=3,
            rules=factories.make_rules(max_offer_ttl_ticks=2),
            me=station(inventory=Bundle(water=30, food=0, components=30)),
            advertisements=(peer_selling(Resource.FOOD),),
        ),
        PolicyMemory(),
    )

    for offer in of_type(decision, OfferAction):
        assert offer.expires_tick == 5


# --- withdrawing ----------------------------------------------------------


def test_an_offer_promising_what_we_now_need_is_withdrawn():
    """Our situation changed; honouring the old terms would hurt survival."""
    risky = factories.make_offer(
        offer_id="risky", proposer_id="P01", recipient_id="P02",
        give=Bundle(food=5), receive=Bundle(water=1), expires_tick=50,
    )
    decision, _ = decide(
        snapshot(me=station(inventory=Bundle(water=30, food=0, components=30)), offers=(risky,)),
        PolicyMemory(),
    )

    assert WithdrawAction("risky") in decision.actions


def test_a_harmless_offer_is_left_to_expire():
    """Expiry costs nothing; a withdrawal costs a command."""
    harmless = factories.make_offer(
        offer_id="fine", proposer_id="P01", recipient_id="P02",
        give=Bundle(water=1), receive=Bundle(food=1), expires_tick=50,
    )
    decision, _ = decide(
        snapshot(me=station(inventory=Bundle(30, 30, 30)), offers=(harmless,)),
        PolicyMemory(),
    )

    assert of_type(decision, WithdrawAction) == []


# --- advertising ----------------------------------------------------------


def test_a_station_with_surplus_and_needs_advertises():
    decision, _ = decide(
        snapshot(me=station(inventory=Bundle(water=30, food=0, components=30))),
        PolicyMemory(),
    )

    ads = of_type(decision, AdvertiseAction)
    assert ads
    assert Resource.WATER in ads[0].selling
    assert Resource.FOOD in ads[0].seeking


# --- altruism -------------------------------------------------------------


def distress_ad(tick, station_id="P03"):
    return factories.make_advertisement(
        station_id=station_id,
        selling=frozenset(),
        seeking=frozenset({Resource.WATER}),
        created_tick=tick,
        expires_tick=tick + 50,
    )


def distressed_state(tick, **kwargs):
    """A state where P03 has been seeking water for long enough to read as distress."""
    return snapshot(tick=tick, advertisements=(distress_ad(tick),), **kwargs)


def distressed_memory(through_tick, station_id="P03"):
    memory = PolicyMemory()
    for t in range(through_tick):
        memory.counterparties.update(
            snapshot(tick=t, advertisements=(distress_ad(t, station_id),))
        )
    return memory


def test_a_comfortable_station_gifts_to_a_struggling_neighbour():
    """All nine must survive, and a gift is an ordinary offer with no price."""
    memory = distressed_memory(through_tick=5)
    decision, _ = decide(
        distressed_state(5, me=station(inventory=Bundle(60, 60, 60))), memory
    )

    gifts = [a for a in of_type(decision, OfferAction) if a.is_gift]
    assert gifts
    assert gifts[0].recipient_id == "P03"
    assert gifts[0].receive.is_zero()
    assert gifts[0].give.water > 0


def test_no_gift_is_sent_while_we_are_short_ourselves():
    memory = distressed_memory(through_tick=5)
    decision, _ = decide(
        distressed_state(5, me=station(inventory=Bundle(water=30, food=0, components=30))),
        memory,
    )

    assert [a for a in of_type(decision, OfferAction) if a.is_gift] == []


def test_a_gift_is_a_bounded_fraction_of_surplus():
    """Generosity must never become our own shortage."""
    memory = distressed_memory(through_tick=5)
    state = distressed_state(5, me=station(inventory=Bundle(60, 60, 60)))

    decision, _ = decide(state, memory)

    gift = [a for a in of_type(decision, OfferAction) if a.is_gift][0]
    assert gift.give.water < decision.surplus.water


def test_a_gift_is_not_repeated_within_the_cooldown():
    memory = distressed_memory(through_tick=5)
    state = distressed_state(5, me=station(inventory=Bundle(60, 60, 60)))

    first, memory = decide(state, memory)
    second, _ = decide(state, memory)

    assert [a for a in of_type(first, OfferAction) if a.is_gift]
    assert [a for a in of_type(second, OfferAction) if a.is_gift] == []


def test_a_need_no_longer_advertised_stops_attracting_help():
    """Distress is inferred from current public signals, not remembered forever."""
    memory = distressed_memory(through_tick=5)

    decision, _ = decide(
        snapshot(tick=5, me=station(inventory=Bundle(60, 60, 60))), memory
    )

    assert [a for a in of_type(decision, OfferAction) if a.is_gift] == []


# --- reporting ------------------------------------------------------------


def test_the_decision_reports_the_figures_it_reasoned_from():
    decision, _ = decide(
        snapshot(me=station(inventory=Bundle(water=30, food=0, components=30))),
        PolicyMemory(),
    )

    assert decision.reserve.water > 0
    assert decision.urgency[Resource.FOOD] is Urgency.CRITICAL
    assert decision.surplus.water > 0
    assert decision.deficit.food > 0
