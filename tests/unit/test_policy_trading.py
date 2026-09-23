"""Advertising, targeting, accepting and altruism.

Our station (P01) produces WATER throughout, so FOOD and COMPONENTS are the
resources it must import.
"""

from __future__ import annotations

from bazaar_client.domain.types import Bundle, OfferStatus, Resource
from bazaar_client.execution.actions import AdvertiseAction
from bazaar_client.policy.accept import evaluate_incoming
from bazaar_client.policy.advertising import MIN_AD_LIFETIME, decide_advertisement
from bazaar_client.policy.altruism import (
    COOLDOWN_TICKS,
    MAX_GIFT_SIZE,
    MAX_GIFTS_PER_TICK,
    PERSISTENCE_TICKS,
    donation_size,
    scan_for_distress,
)
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.pricing import TRADE_SIZE_MAX
from bazaar_client.policy.targeting import (
    BACKOFF_AFTER_EXPIRIES,
    BACKOFF_TICKS,
    MAX_OFFERS_PER_NEED,
    is_backed_off,
    rank_counterparties,
    score_counterparty,
)
from bazaar_client.world.counterparties import CounterpartyModel, CounterpartyStats
from tests.fixtures import factories

WATER, FOOD, COMPONENTS = Resource.WATER, Resource.FOOD, Resource.COMPONENTS
IMPORTS = frozenset({FOOD, COMPONENTS})
RULES = factories.make_rules()


def ad(**kwargs):
    return factories.make_advertisement(**kwargs)


# --- advertising ----------------------------------------------------------


def test_the_advertisement_sells_our_specialty_and_seeks_both_imports():
    decision = decide_advertisement(WATER, spendable=10, current=None, tick=0, rules=RULES)

    assert decision.action == AdvertiseAction(
        frozenset({WATER}), IMPORTS, RULES.max_publication_ttl_ticks
    )


def test_imports_are_always_sought_because_we_consume_them_every_tick():
    """Even with full stock: advertising a need only once it bites is too late."""
    decision = decide_advertisement(WATER, spendable=50, current=None, tick=0, rules=RULES)

    assert decision.action.seeking == IMPORTS


def test_nothing_is_offered_for_sale_when_no_specialty_can_be_spared():
    decision = decide_advertisement(WATER, spendable=0, current=None, tick=0, rules=RULES)

    assert decision.action.selling == frozenset()
    assert decision.action.seeking == IMPORTS


def test_an_accurate_advertisement_is_left_alone():
    """Republishing an unchanged ad burns budget and tells partners nothing."""
    current = ad(selling=frozenset({WATER}), seeking=IMPORTS, created_tick=0, expires_tick=10)

    assert decide_advertisement(WATER, 10, current, tick=3, rules=RULES).action is None


def test_a_young_advertisement_is_not_replaced_for_a_flicker_in_spare_stock():
    current = ad(selling=frozenset({WATER}), seeking=IMPORTS, created_tick=0, expires_tick=10)

    decision = decide_advertisement(WATER, 0, current, tick=MIN_AD_LIFETIME - 1, rules=RULES)

    assert decision.action is None


def test_an_advertisement_is_updated_once_spare_stock_really_changes():
    current = ad(selling=frozenset({WATER}), seeking=IMPORTS, created_tick=0, expires_tick=10)

    decision = decide_advertisement(WATER, 0, current, tick=MIN_AD_LIFETIME, rules=RULES)

    assert decision.action.selling == frozenset()


def test_an_advertisement_is_renewed_before_it_expires():
    current = ad(selling=frozenset({WATER}), seeking=IMPORTS, created_tick=0, expires_tick=6)

    assert isinstance(decide_advertisement(WATER, 10, current, tick=5, rules=RULES).action,
                      AdvertiseAction)


def test_advertisement_lifetime_is_capped_by_the_rules_and_the_run():
    short_ttl = factories.make_rules(max_publication_ttl_ticks=3)
    assert decide_advertisement(WATER, 5, None, tick=10, rules=short_ttl).action.expires_tick == 13

    ending = factories.make_rules(duration_ticks=12)
    assert decide_advertisement(WATER, 5, None, tick=10, rules=ending).action.expires_tick == 12


def test_no_advertisement_once_the_run_has_no_ticks_left():
    ending = factories.make_rules(duration_ticks=10)

    assert decide_advertisement(WATER, 5, None, tick=10, rules=ending).action is None


# --- targeting ------------------------------------------------------------


def stats(**kwargs):
    defaults = dict(station_id="P02", last_ad_tick=0, ever_active=True)
    return CounterpartyStats(**{**defaults, **kwargs})


def test_a_seller_of_what_we_need_outranks_a_stranger():
    seller = stats(station_id="P02", selling=frozenset({FOOD}))
    stranger = stats(station_id="P03")

    assert score_counterparty(seller, FOOD, WATER, 0) > score_counterparty(stranger, FOOD, WATER, 0)


def test_a_partner_that_has_supplied_it_before_outranks_a_stranger():
    supplier = stats(supplied={FOOD})

    assert score_counterparty(supplier, FOOD, WATER, 0) > score_counterparty(stats(), FOOD, WATER, 0)


def test_wanting_what_we_pay_with_also_raises_the_score():
    wants_water = stats(seeking=frozenset({WATER}))

    assert score_counterparty(wants_water, FOOD, WATER, 0) > score_counterparty(stats(), FOOD, WATER, 0)


def test_a_reliable_partner_outranks_an_unreliable_one():
    reliable = stats(offers_sent_to=4, offers_accepted_by=4)
    flaky = stats(offers_sent_to=4, offers_accepted_by=0)

    assert score_counterparty(reliable, FOOD, WATER, 0) > score_counterparty(flaky, FOOD, WATER, 0)


def test_a_failed_station_is_never_a_candidate():
    """A failed planet cannot trade, so an offer to it is a wasted command."""
    assert score_counterparty(stats(station_failed=True), FOOD, WATER, 0) == float("-inf")


def model_of(*station_stats):
    model = CounterpartyModel()
    for s in station_stats:
        model._stations[s.station_id] = s
    return model


def test_every_offer_pays_with_our_specialty():
    model = model_of(stats(station_id="P02", selling=frozenset({FOOD, COMPONENTS})))

    candidates = rank_counterparties(model, WATER, {FOOD: 5, COMPONENTS: 5}, tick=0)

    assert candidates and all(c.give is WATER for c in candidates)


def test_our_own_specialty_is_never_wanted():
    model = model_of(stats(selling=frozenset({WATER})))

    assert rank_counterparties(model, WATER, {WATER: 9}, tick=0) == []


def test_the_largest_shortfall_is_addressed_first():
    model = model_of(stats(station_id="P02"))

    candidates = rank_counterparties(model, WATER, {FOOD: 3, COMPONENTS: 12}, tick=0)

    assert candidates[0].want is COMPONENTS


def test_planets_with_evidence_of_producing_it_crowd_out_guesses():
    """Asking a planet that has never shown any food for food is a wasted command."""
    model = model_of(
        stats(station_id="P02", selling=frozenset({FOOD})),
        stats(station_id="P03"),
    )

    candidates = rank_counterparties(model, WATER, {FOOD: 5}, tick=0)

    assert [c.station_id for c in candidates] == ["P02"]


def test_with_no_evidence_anywhere_active_planets_are_still_tried():
    model = model_of(stats(station_id="P02"), stats(station_id="P03"))

    assert len(rank_counterparties(model, WATER, {FOOD: 5}, tick=0)) == MAX_OFFERS_PER_NEED


def test_a_planet_that_never_showed_a_running_client_is_skipped():
    """Run 2's P02 never traded at all; every offer sent to it expired."""
    model = model_of(
        stats(station_id="P02", ever_active=False, last_ad_tick=-1),
        stats(station_id="P03"),
    )

    assert [c.station_id for c in rank_counterparties(model, WATER, {FOOD: 5}, tick=0)] == ["P03"]


def test_at_most_two_partners_are_approached_per_need():
    model = model_of(*(stats(station_id=f"P0{i}", selling=frozenset({FOOD})) for i in range(2, 7)))

    candidates = rank_counterparties(model, WATER, {FOOD: 5}, tick=0)

    assert len(candidates) == MAX_OFFERS_PER_NEED


def test_every_need_gets_its_first_choice_before_any_second_choice():
    model = model_of(
        stats(station_id="P02", selling=frozenset({FOOD, COMPONENTS})),
        stats(station_id="P03", selling=frozenset({FOOD, COMPONENTS})),
    )

    wants = [c.want for c in rank_counterparties(model, WATER, {FOOD: 9, COMPONENTS: 5}, tick=0)]

    assert wants == [FOOD, COMPONENTS, FOOD, COMPONENTS]


def test_a_pairing_we_already_have_an_open_offer_for_is_skipped():
    """A standing offer still promises that stock; repeating it strangles us."""
    model = model_of(stats(station_id="P02"), stats(station_id="P03"))

    candidates = rank_counterparties(
        model, WATER, {FOOD: 5}, tick=0, already_pending=frozenset({("P02", FOOD)})
    )

    assert [c.station_id for c in candidates] == ["P03"]


def test_open_offers_count_toward_the_limit_per_need():
    model = model_of(stats(station_id="P04"))
    pending = frozenset({("P02", FOOD), ("P03", FOOD)})

    assert rank_counterparties(model, WATER, {FOOD: 5}, tick=0, already_pending=pending) == []


def test_a_partner_that_ignores_even_one_unit_offers_is_rested():
    quiet = stats(consecutive_expired=BACKOFF_AFTER_EXPIRIES, last_expired_tick=10, size_limit=1)

    assert is_backed_off(quiet, tick=10 + BACKOFF_TICKS - 1)
    assert not is_backed_off(quiet, tick=10 + BACKOFF_TICKS)
    assert rank_counterparties(model_of(quiet), WATER, {FOOD: 5}, tick=11) == []


def test_a_partner_short_of_stock_is_asked_for_less_rather_than_dropped():
    """It may be the only supplier left; resting it would cut the supply off."""
    short = stats(consecutive_expired=BACKOFF_AFTER_EXPIRIES, last_expired_tick=10, size_limit=5)

    assert not is_backed_off(short, tick=11)
    [candidate] = rank_counterparties(model_of(short), WATER, {FOOD: 30}, tick=11)
    assert candidate.size_limit == 5


def test_an_unknown_partner_may_be_asked_for_a_full_size_trade():
    [candidate] = rank_counterparties(model_of(stats()), WATER, {FOOD: 30}, tick=0)

    assert candidate.size_limit == TRADE_SIZE_MAX


# --- counterparty evidence --------------------------------------------------


def test_what_a_partner_hands_us_is_remembered_as_evidence_of_production():
    model = CounterpartyModel()
    model.update(
        factories.make_snapshot(
            transactions=(
                factories.make_transaction(
                    proposer_id="P01", recipient_id="P02",
                    give=Bundle(water=3), receive=Bundle(food=3),
                ),
            ),
        )
    )

    assert model.get("P02").supplied == {FOOD}
    assert model.get("P02").ever_active


def test_consecutive_expiries_are_counted_and_an_acceptance_resets_them():
    model = CounterpartyModel()

    def our_offer(offer_id, status, tick):
        return factories.make_offer(
            offer_id=offer_id, proposer_id="P01", recipient_id="P02",
            status=status, closed_tick=tick,
        )

    model.update(factories.make_snapshot(offers=(
        our_offer("o1", OfferStatus.EXPIRED, 5), our_offer("o2", OfferStatus.EXPIRED, 7),
    )))
    assert model.get("P02").consecutive_expired == 2
    assert model.get("P02").last_expired_tick == 7

    model.update(factories.make_snapshot(offers=(our_offer("o3", OfferStatus.ACCEPTED, 8),)))
    assert model.get("P02").consecutive_expired == 0


def test_a_lapsed_offer_halves_what_we_ask_and_an_accepted_one_doubles_it():
    model = CounterpartyModel()

    def our_offer(offer_id, status, asked):
        return factories.make_offer(
            offer_id=offer_id, proposer_id="P01", recipient_id="P02", status=status,
            give=Bundle(water=asked), receive=Bundle(food=asked), closed_tick=1,
        )

    model.update(factories.make_snapshot(offers=(our_offer("o1", OfferStatus.EXPIRED, 20),)))
    assert model.get("P02").size_limit == 10

    model.update(factories.make_snapshot(offers=(our_offer("o2", OfferStatus.EXPIRED, 1),)))
    assert model.get("P02").size_limit == 1

    model.update(factories.make_snapshot(offers=(our_offer("o3", OfferStatus.ACCEPTED, 1),)))
    assert model.get("P02").size_limit == 2


def test_a_declined_gift_says_nothing_about_trade_size():
    model = CounterpartyModel()
    gift = factories.make_offer(
        offer_id="g1", proposer_id="P01", recipient_id="P02", status=OfferStatus.EXPIRED,
        give=Bundle(water=3), receive=Bundle.zero(), closed_tick=1,
    )

    model.update(factories.make_snapshot(offers=(gift,)))

    assert model.get("P02").size_limit is None


# --- accepting ------------------------------------------------------------


def incoming(give, receive):
    """An offer from P02: `give` is what we would get, `receive` what we would pay."""
    return factories.make_offer(proposer_id="P02", recipient_id="P01", give=give, receive=receive)


def test_a_gift_is_always_accepted():
    """It costs nothing, and the protocol still requires an explicit accept."""
    verdict = evaluate_incoming(incoming(Bundle(food=1), Bundle.zero()), "P01", WATER, spendable=0)

    assert verdict.accept


def test_our_specialty_for_an_import_one_for_one_is_accepted():
    verdict = evaluate_incoming(
        incoming(Bundle(food=20), Bundle(water=20)), "P01", WATER, spendable=30
    )

    assert verdict.accept


def test_getting_more_than_we_pay_is_accepted():
    verdict = evaluate_incoming(incoming(Bundle(food=5), Bundle(water=3)), "P01", WATER, spendable=30)

    assert verdict.accept


def test_an_offer_asking_more_than_it_gives_is_declined():
    """Never give more of one resource to get less of another."""
    verdict = evaluate_incoming(incoming(Bundle(food=2), Bundle(water=3)), "P01", WATER, spendable=30)

    assert not verdict.accept
    assert "more than it gives" in verdict.reason


def test_paying_with_a_resource_we_cannot_produce_is_declined():
    """Run 2: paying water for food at tick 0 swapped one scarce import for another."""
    verdict = evaluate_incoming(
        incoming(Bundle(components=3), Bundle(food=3)), "P01", WATER, spendable=30
    )

    assert not verdict.accept
    assert "cannot produce" in verdict.reason


def test_paying_beyond_our_spendable_specialty_is_declined():
    verdict = evaluate_incoming(incoming(Bundle(food=9), Bundle(water=9)), "P01", WATER, spendable=5)

    assert not verdict.accept
    assert "reserve" in verdict.reason


def test_an_offer_that_brings_nothing_we_import_is_declined():
    verdict = evaluate_incoming(incoming(Bundle(water=5), Bundle(water=5)), "P01", WATER, spendable=30)

    assert not verdict.accept


# --- altruism -------------------------------------------------------------


def distressed_model(streak=PERSISTENCE_TICKS, station_id="P02", seeking=WATER):
    model = CounterpartyModel()
    for tick in range(streak):
        model.update(
            factories.make_snapshot(
                tick=tick,
                self_station_id="P01",
                advertisements=(
                    ad(station_id=station_id, seeking=frozenset({seeking}), expires_tick=99),
                ),
            )
        )
    return model


def test_a_planet_that_keeps_seeking_our_specialty_is_helped():
    intents = scan_for_distress(distressed_model(), WATER, excess=30, tick=5, memory=PolicyMemory())

    assert [(g.station_id, g.resource) for g in intents] == [("P02", WATER)]


def test_gifts_are_only_ever_our_specialty():
    """Run 2: gifting water we could not produce left us short of it."""
    intents = scan_for_distress(
        distressed_model(seeking=FOOD), WATER, excess=30, tick=5, memory=PolicyMemory()
    )

    assert intents == []


def test_a_brief_need_is_not_treated_as_distress():
    """Ordinary trading rhythm should not trigger charity."""
    intents = scan_for_distress(
        distressed_model(streak=PERSISTENCE_TICKS - 1), WATER, 30, tick=5, memory=PolicyMemory()
    )

    assert intents == []


def test_nothing_is_given_without_stock_above_the_gift_floor():
    assert scan_for_distress(distressed_model(), WATER, excess=0, tick=5, memory=PolicyMemory()) == []


def test_a_donation_is_a_bounded_fraction_of_idle_stock():
    assert donation_size(10) == 2
    assert donation_size(3) == 1  # always enough to be worth sending
    assert donation_size(10_000) == MAX_GIFT_SIZE


def test_the_same_station_is_not_given_to_repeatedly():
    """A cooldown spreads help rather than adopting one planet."""
    memory = PolicyMemory()
    memory.record_gift("P02", WATER, tick=5)

    assert scan_for_distress(
        distressed_model(), WATER, 30, tick=5 + COOLDOWN_TICKS - 1, memory=memory
    ) == []
    assert scan_for_distress(distressed_model(), WATER, 30, tick=5 + COOLDOWN_TICKS, memory=memory)


def test_at_most_one_gift_is_sent_per_tick():
    model = CounterpartyModel()
    for tick in range(PERSISTENCE_TICKS):
        model.update(
            factories.make_snapshot(
                tick=tick,
                self_station_id="P01",
                advertisements=(
                    ad(advertisement_id="a2", station_id="P02",
                       seeking=frozenset({WATER}), expires_tick=99),
                    ad(advertisement_id="a3", station_id="P03",
                       seeking=frozenset({WATER}), expires_tick=99),
                ),
            )
        )

    assert len(scan_for_distress(model, WATER, 30, tick=5, memory=PolicyMemory())) == MAX_GIFTS_PER_TICK
