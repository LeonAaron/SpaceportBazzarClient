"""Production supply overrides import urgency on every spending path."""

import pytest

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.execution.actions import AcceptAction, OfferAction, WithdrawAction
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.policy.pricing import CannotAfford, compute_terms
from bazaar_client.policy.reserves import Urgency, production_payment_limit
from bazaar_client.world.commitments import CommitmentTracker
from tests.fixtures import factories as f


@pytest.mark.parametrize('resource', list(Resource))
@pytest.mark.parametrize('stock, expected', [(19, 0), (20, .5), (49, .5), (50, None)])
def test_thresholds_use_ticks_of_upkeep_for_each_specialty(resource, stock, expected):
    station = f.make_station(specialty=resource, upkeep_per_tick=Bundle(2, 2, 2))
    assert production_payment_limit(Bundle.single(resource, stock), station) == expected


@pytest.mark.parametrize('stock', [0, 9, 12, 24, 60])
def test_outgoing_trade_prices_obey_production_supply(stock):
    state = f.make_snapshot(
        me=f.make_station(inventory=Bundle(stock, 0, 40)),
        advertisements=(f.make_advertisement(station_id='P02',
            selling=frozenset({Resource.FOOD}), seeking=frozenset({Resource.WATER})),),
    )
    decision, _ = decide(state, PolicyMemory())
    offers = [a for a in decision.actions if isinstance(a, OfferAction)]
    if stock < 10:
        assert not offers
    else:
        offer = next(a for a in offers if a.receive.food)
        assert offer.give.water / offer.receive.food == (.5 if stock < 25 else 8)


@pytest.mark.parametrize('stock, cost, accepted', [
    (9, 1, False), (9, 0, True), (12, 2, True), (24, 3, False), (40, 12, True),
    (10, 2, False), (26, 4, False),
])
def test_incoming_trade_guard_and_free_gifts(stock, cost, accepted):
    offer = f.make_offer(proposer_id='P02', recipient_id='P01',
                         give=Bundle(food=4), receive=Bundle(water=cost))
    decision, _ = decide(f.make_snapshot(
        me=f.make_station(inventory=Bundle(stock, 0, 40)), offers=(offer,)), PolicyMemory())
    assert (AcceptAction(offer.offer_id) in decision.actions) is accepted


def test_inflight_commitments_count_toward_supply_guard():
    tracker = CommitmentTracker()
    tracker.register_inflight('pending', Bundle(water=32))
    decision, _ = decide(f.make_snapshot(me=f.make_station(inventory=Bundle(40, 0, 40))),
                         PolicyMemory(), tracker)
    assert not any(isinstance(a, OfferAction) for a in decision.actions)


@pytest.mark.parametrize('cost, withdraw', [(2, False), (4, True)])
def test_standing_offer_must_meet_half_price_below_twenty_five(cost, withdraw):
    offer = f.make_offer(give=Bundle(water=cost), receive=Bundle(food=4))
    decision, _ = decide(f.make_snapshot(
        me=f.make_station(inventory=Bundle(24, 40, 40)), offers=(offer,)), PolicyMemory())
    assert (WithdrawAction(offer.offer_id) in decision.actions) is withdraw


def test_half_price_does_not_round_up_to_one_for_one():
    with pytest.raises(CannotAfford):
        compute_terms(Resource.FOOD, 1, Resource.WATER, Bundle(water=30),
                      Urgency.CRITICAL, max_payment_ratio=.5)


@pytest.mark.parametrize("stock", [10, 11, 12, 24, 25, 26, 30, 35, 60])
def test_posting_offers_does_not_trigger_immediate_withdrawal(stock):
    from dataclasses import replace
    state = f.make_snapshot(me=f.make_station(inventory=Bundle(stock, 6, 15)),
        advertisements=(f.make_advertisement(station_id="P02",
            selling=frozenset({Resource.FOOD, Resource.COMPONENTS})),))
    decision, memory = decide(state, PolicyMemory())
    offers = tuple(f.make_offer(offer_id=f"posted-{i}", recipient_id=a.recipient_id,
                   give=a.give, receive=a.receive, expires_tick=a.expires_tick)
                   for i, a in enumerate(decision.actions) if isinstance(a, OfferAction))
    after, _ = decide(replace(state, snapshot_sequence=2, offers=offers), memory)
    assert not any(isinstance(a, WithdrawAction) and a.object_id.startswith("posted-")
                   for a in after.actions)


def test_new_half_price_offer_cannot_invalidate_an_existing_normal_offer():
    standing = f.make_offer(give=Bundle(water=4), receive=Bundle(food=4))
    decision, _ = decide(f.make_snapshot(
        me=f.make_station(inventory=Bundle(30, 30, 30)), offers=(standing,)), PolicyMemory())
    new_cost = sum(a.give.water for a in decision.actions if isinstance(a, OfferAction))
    assert 30 - standing.give.water - new_cost >= 25
