"""Advertising, targeting, accepting and altruism."""

from __future__ import annotations

import pytest

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.execution.actions import AdvertiseAction, WithdrawAction
from bazaar_client.policy.accept import evaluate_incoming
from bazaar_client.policy.advertising import (
    MIN_LISTABLE_QTY,
    decide_advertisement,
)
from bazaar_client.policy.altruism import (
    COOLDOWN_TICKS,
    MAX_GIFTS_PER_TICK,
    PERSISTENCE_TICKS,
    donation_size,
    scan_for_distress,
)
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.reserves import Urgency
from bazaar_client.policy.targeting import rank_counterparties, score_counterparty
from bazaar_client.world.counterparties import CounterpartyModel, CounterpartyStats
from tests.fixtures import factories

NO_URGENCY = {r: Urgency.NONE for r in Resource}
RULES = factories.make_rules()


def ad(**kwargs):
    return factories.make_advertisement(**kwargs)


# --- advertising ----------------------------------------------------------


def test_a_first_advertisement_lists_surplus_and_needs():
    decision = decide_advertisement(
        Bundle(water=5), Bundle(food=2), frozenset(), None, tick=0, rules=RULES
    )

    assert decision.action == AdvertiseAction(
        frozenset({Resource.WATER}), frozenset({Resource.FOOD}), 8
    )


def test_a_single_unit_surplus_is_not_worth_listing():
    decision = decide_advertisement(
        Bundle(water=MIN_LISTABLE_QTY - 1), Bundle.zero(), frozenset(), None, 0, RULES
    )

    assert decision.action is None


def test_an_accurate_advertisement_is_left_alone():
    """Replacing it every tick would burn budget and say nothing new."""
    current = ad(
        selling=frozenset({Resource.WATER}),
        seeking=frozenset({Resource.FOOD}),
        created_tick=0,
        expires_tick=20,
    )
    decision = decide_advertisement(
        Bundle(water=5), Bundle(food=2), frozenset(), current, tick=1, rules=RULES
    )

    assert decision.action is None


def test_a_young_advertisement_is_not_replaced_for_a_minor_change():
    current = ad(
        selling=frozenset({Resource.WATER}), seeking=frozenset({Resource.FOOD}),
        created_tick=0, expires_tick=20,
    )
    decision = decide_advertisement(
        Bundle(water=5), Bundle(components=1), frozenset(), current, tick=1, rules=RULES
    )

    assert decision.action is None


def test_a_settled_advertisement_is_replaced_once_needs_really_change():
    current = ad(
        selling=frozenset({Resource.WATER}), seeking=frozenset({Resource.FOOD}),
        created_tick=0, expires_tick=20,
    )
    decision = decide_advertisement(
        Bundle(water=5), Bundle(components=1), frozenset(), current, tick=5, rules=RULES
    )

    assert decision.action.seeking == frozenset({Resource.COMPONENTS})


def test_a_new_critical_need_overrides_the_churn_guard():
    """Waiting three ticks to say we are starving would be too slow."""
    current = ad(
        selling=frozenset({Resource.WATER}), seeking=frozenset({Resource.FOOD}),
        created_tick=0, expires_tick=20,
    )
    decision = decide_advertisement(
        Bundle(water=5), Bundle(components=1), frozenset({Resource.COMPONENTS}),
        current, tick=1, rules=RULES,
    )

    assert Resource.COMPONENTS in decision.action.seeking
    assert decision.reason == "new critical need"


def test_an_advertisement_is_renewed_before_it_expires():
    current = ad(
        selling=frozenset({Resource.WATER}), seeking=frozenset({Resource.FOOD}),
        created_tick=0, expires_tick=6,
    )
    decision = decide_advertisement(
        Bundle(water=5), Bundle(food=2), frozenset(), current, tick=5, rules=RULES
    )

    assert isinstance(decision.action, AdvertiseAction)


def test_having_nothing_to_say_withdraws_the_listing():
    current = ad(created_tick=0, expires_tick=20)
    decision = decide_advertisement(
        Bundle.zero(), Bundle.zero(), frozenset(), current, tick=5, rules=RULES
    )

    assert decision.action == WithdrawAction(current.advertisement_id)


def test_advertisement_ttl_respects_the_published_cap():
    rules = factories.make_rules(max_publication_ttl_ticks=3)
    decision = decide_advertisement(
        Bundle(water=5), Bundle.zero(), frozenset(), None, tick=10, rules=rules
    )

    assert decision.action.expires_tick == 13


# --- targeting ------------------------------------------------------------


def stats(**kwargs):
    defaults = dict(station_id="P02", last_ad_tick=0)
    return CounterpartyStats(**{**defaults, **kwargs})


def test_a_seller_of_what_we_need_outranks_a_stranger():
    seller = stats(station_id="P02", selling=frozenset({Resource.FOOD}))
    stranger = stats(station_id="P03")

    assert score_counterparty(seller, Resource.FOOD, Resource.WATER, 0) > score_counterparty(
        stranger, Resource.FOOD, Resource.WATER, 0
    )


def test_wanting_what_we_are_paying_with_also_raises_the_score():
    wants_water = stats(seeking=frozenset({Resource.WATER}))

    assert score_counterparty(wants_water, Resource.FOOD, Resource.WATER, 0) > score_counterparty(
        stats(), Resource.FOOD, Resource.WATER, 0
    )


def test_a_stale_advertisement_counts_for_less():
    fresh = stats(last_ad_tick=10)
    stale = stats(last_ad_tick=0)

    assert score_counterparty(fresh, Resource.FOOD, Resource.WATER, 10) > score_counterparty(
        stale, Resource.FOOD, Resource.WATER, 10
    )


def test_a_reliable_partner_outranks_an_unreliable_one():
    reliable = stats(offers_sent_to=4, offers_accepted_by=4)
    flaky = stats(offers_sent_to=4, offers_accepted_by=0)

    assert score_counterparty(reliable, Resource.FOOD, Resource.WATER, 0) > score_counterparty(
        flaky, Resource.FOOD, Resource.WATER, 0
    )


def test_a_failed_station_is_never_a_candidate():
    """A failed planet cannot trade, so an offer to it is a wasted command."""
    assert score_counterparty(
        stats(station_failed=True), Resource.FOOD, Resource.WATER, 0
    ) == float("-inf")


def build_model(*advertisements):
    model = CounterpartyModel()
    model.update(
        factories.make_snapshot(tick=0, advertisements=advertisements, self_station_id="P01")
    )
    return model


def test_ranking_picks_a_partner_for_each_unmet_need():
    model = build_model(
        ad(station_id="P02", selling=frozenset({Resource.FOOD}),
           seeking=frozenset({Resource.WATER}), expires_tick=99)
    )
    candidates = rank_counterparties(
        model,
        {Resource.FOOD: Urgency.CRITICAL, Resource.WATER: Urgency.NONE,
         Resource.COMPONENTS: Urgency.NONE},
        wanted={Resource.FOOD: 3},
        surplus=Bundle(water=5),
        tick=0,
    )

    assert [(c.station_id, c.want, c.give) for c in candidates] == [
        ("P02", Resource.FOOD, Resource.WATER)
    ]


def test_the_most_urgent_need_is_addressed_first():
    model = build_model(ad(station_id="P02", expires_tick=99))
    candidates = rank_counterparties(
        model,
        {Resource.FOOD: Urgency.WATCH, Resource.COMPONENTS: Urgency.CRITICAL,
         Resource.WATER: Urgency.NONE},
        wanted={Resource.FOOD: 2, Resource.COMPONENTS: 2},
        surplus=Bundle(water=9),
        tick=0,
    )

    assert candidates[0].want is Resource.COMPONENTS


def test_nothing_is_proposed_without_surplus_to_pay_with():
    model = build_model(ad(station_id="P02", expires_tick=99))

    assert rank_counterparties(
        model, {r: Urgency.CRITICAL for r in Resource}, {Resource.FOOD: 3}, Bundle.zero(), 0
    ) == []


def test_nothing_is_proposed_when_nothing_is_needed():
    model = build_model(ad(station_id="P02", expires_tick=99))

    assert rank_counterparties(model, NO_URGENCY, {}, Bundle(water=9), 0) == []


def test_a_pairing_we_already_have_an_open_offer_for_is_skipped():
    """A standing offer still promises that stock; repeating it strangles us."""
    model = build_model(ad(station_id="P02", expires_tick=99))

    candidates = rank_counterparties(
        model,
        {Resource.FOOD: Urgency.CRITICAL, Resource.WATER: Urgency.NONE,
         Resource.COMPONENTS: Urgency.NONE},
        wanted={Resource.FOOD: 3},
        surplus=Bundle(water=9),
        tick=0,
        already_pending=frozenset({("P02", Resource.FOOD)}),
    )

    assert candidates == []


def test_another_station_is_still_approachable_for_the_same_resource():
    model = build_model(
        ad(advertisement_id="a2", station_id="P02", expires_tick=99),
        ad(advertisement_id="a3", station_id="P03", expires_tick=99),
    )

    candidates = rank_counterparties(
        model,
        {Resource.FOOD: Urgency.CRITICAL, Resource.WATER: Urgency.NONE,
         Resource.COMPONENTS: Urgency.NONE},
        wanted={Resource.FOOD: 3},
        surplus=Bundle(water=9),
        tick=0,
        already_pending=frozenset({("P02", Resource.FOOD)}),
    )

    assert [c.station_id for c in candidates] == ["P03"]


# --- accepting ------------------------------------------------------------


def incoming(give, receive, **kwargs):
    return factories.make_offer(
        proposer_id="P02", recipient_id="P01", give=give, receive=receive, **kwargs
    )


def test_a_gift_is_always_accepted():
    """It costs nothing, and the protocol still requires an explicit accept."""
    verdict = evaluate_incoming(
        incoming(Bundle(components=1), Bundle.zero()),
        "P01", Bundle.zero(), Bundle(3, 3, 3), NO_URGENCY,
    )

    assert verdict.accept


def test_a_fair_trade_is_accepted():
    verdict = evaluate_incoming(
        incoming(Bundle(food=2), Bundle(water=2)),
        "P01", Bundle(10, 10, 10), Bundle(3, 3, 3), NO_URGENCY,
    )

    assert verdict.accept


def test_an_offer_we_cannot_pay_for_is_declined():
    """Accepting would move nothing and waste the command."""
    verdict = evaluate_incoming(
        incoming(Bundle(food=2), Bundle(water=50)),
        "P01", Bundle(10, 10, 10), Bundle(3, 3, 3), NO_URGENCY,
    )

    assert not verdict.accept
    assert "cannot pay" in verdict.reason


def test_a_lopsided_trade_is_declined_when_nothing_is_needed():
    verdict = evaluate_incoming(
        incoming(Bundle(food=1), Bundle(water=5)),
        "P01", Bundle(10, 10, 10), Bundle(3, 3, 3), NO_URGENCY,
    )

    assert not verdict.accept


def test_a_lopsided_trade_is_taken_when_it_answers_a_critical_need():
    """Survival outranks price: health lost cannot be bought back."""
    urgency = {**NO_URGENCY, Resource.FOOD: Urgency.CRITICAL}
    verdict = evaluate_incoming(
        incoming(Bundle(food=2), Bundle(water=3)),
        "P01", Bundle(10, 1, 10), Bundle(3, 3, 3), urgency,
    )

    assert verdict.accept


def test_a_trade_that_would_break_our_reserve_is_declined():
    verdict = evaluate_incoming(
        incoming(Bundle(components=4), Bundle(water=9)),
        "P01", Bundle(10, 10, 10), Bundle(5, 5, 5), NO_URGENCY,
    )

    assert not verdict.accept
    assert "reserve" in verdict.reason


# --- altruism -------------------------------------------------------------


def distressed_model(streak=PERSISTENCE_TICKS, station_id="P02"):
    model = CounterpartyModel()
    for tick in range(streak):
        model.update(
            factories.make_snapshot(
                tick=tick,
                self_station_id="P01",
                advertisements=(
                    ad(station_id=station_id, seeking=frozenset({Resource.WATER}),
                       expires_tick=99),
                ),
            )
        )
    return model


def test_a_sustained_unmet_need_attracts_help():
    intents = scan_for_distress(
        distressed_model(), Bundle(water=10), NO_URGENCY, tick=5, memory=PolicyMemory()
    )

    assert [(g.station_id, g.resource) for g in intents] == [("P02", Resource.WATER)]


def test_a_brief_need_is_not_treated_as_distress():
    """Ordinary trading rhythm should not trigger charity."""
    intents = scan_for_distress(
        distressed_model(streak=PERSISTENCE_TICKS - 1),
        Bundle(water=10), NO_URGENCY, tick=5, memory=PolicyMemory(),
    )

    assert intents == []


def test_no_help_is_given_while_anything_of_ours_is_urgent():
    """Our own upkeep comes first; a dead planet helps nobody."""
    urgency = {**NO_URGENCY, Resource.FOOD: Urgency.WATCH}

    assert scan_for_distress(
        distressed_model(), Bundle(water=10), urgency, tick=5, memory=PolicyMemory()
    ) == []


def test_help_only_comes_from_surplus_above_our_reserve():
    assert scan_for_distress(
        distressed_model(), Bundle.zero(), NO_URGENCY, tick=5, memory=PolicyMemory()
    ) == []


def test_a_donation_is_a_bounded_fraction_of_idle_surplus():
    assert donation_size(10) == 2
    assert donation_size(3) == 1  # always enough to be worth sending


def test_the_same_station_is_not_given_to_repeatedly():
    """A cooldown spreads help rather than adopting one planet."""
    memory = PolicyMemory()
    memory.record_gift("P02", Resource.WATER, tick=5)

    assert scan_for_distress(
        distressed_model(), Bundle(water=10), NO_URGENCY,
        tick=5 + COOLDOWN_TICKS - 1, memory=memory,
    ) == []


def test_help_resumes_once_the_cooldown_passes():
    memory = PolicyMemory()
    memory.record_gift("P02", Resource.WATER, tick=5)

    assert scan_for_distress(
        distressed_model(), Bundle(water=10), NO_URGENCY,
        tick=5 + COOLDOWN_TICKS, memory=memory,
    )


def test_at_most_one_gift_is_sent_per_tick():
    model = CounterpartyModel()
    for tick in range(PERSISTENCE_TICKS):
        model.update(
            factories.make_snapshot(
                tick=tick,
                self_station_id="P01",
                directory=(
                    factories.DirectoryEntry("P02", "Two"),
                    factories.DirectoryEntry("P03", "Three"),
                ),
                advertisements=(
                    ad(advertisement_id="a2", station_id="P02",
                       seeking=frozenset({Resource.WATER}), expires_tick=99),
                    ad(advertisement_id="a3", station_id="P03",
                       seeking=frozenset({Resource.WATER}), expires_tick=99),
                ),
            )
        )

    intents = scan_for_distress(
        model, Bundle(water=10), NO_URGENCY, tick=5, memory=PolicyMemory()
    )

    assert len(intents) == MAX_GIFTS_PER_TICK
