"""Well-stocked stations seek profitable imports without advertising."""

import pytest

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.execution.actions import AcceptAction, AdvertiseAction, OfferAction, WithdrawAction
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.world.commitments import CommitmentTracker
from tests.fixtures import factories as f


@pytest.mark.parametrize('inventory, advertises', [
    (Bundle(31, 31, 31), False), (Bundle(100, 100, 100), False),
    (Bundle(30, 40, 40), True), (Bundle(40, 30, 40), True), (Bundle(40, 40, 30), True),
])
def test_all_resources_must_be_strictly_above_thirty(inventory, advertises):
    decision, _ = decide(f.make_snapshot(me=f.make_station(inventory=inventory)), PolicyMemory())
    assert any(isinstance(a, AdvertiseAction) for a in decision.actions) is advertises


@pytest.mark.parametrize('specialty', list(Resource))
@pytest.mark.parametrize('kind, accepted', [
    ('profitable', True), ('parity', False), ('loss', False),
    ('production', False), ('mixed', False), ('import_gift', True), ('production_gift', False),
])
def test_only_profitable_imports_are_accepted_when_well_stocked(specialty, kind, accepted):
    imported = next(r for r in Resource if r != specialty)
    gain = Bundle.single(imported, 4)
    cost = Bundle.single(specialty, 3)
    if kind == 'parity':
        cost = Bundle.single(specialty, 4)
    elif kind == 'loss':
        cost = Bundle.single(specialty, 5)
    elif kind == 'production':
        gain, cost = Bundle.single(specialty, 4), Bundle.single(imported, 1)
    elif kind == 'mixed':
        gain += Bundle.single(specialty, 1)
    elif kind == 'import_gift':
        cost = Bundle.zero()
    elif kind == 'production_gift':
        gain, cost = Bundle.single(specialty, 4), Bundle.zero()
    offer = f.make_offer(proposer_id='P02', recipient_id='P01', give=gain, receive=cost)
    decision, _ = decide(f.make_snapshot(
        me=f.make_station(specialty=specialty, inventory=Bundle(60, 60, 60)),
        offers=(offer,)), PolicyMemory())
    assert (AcceptAction(offer.offer_id) in decision.actions) is accepted


def test_good_deal_precedes_advertisement_withdrawal_and_search_continues():
    offer = f.make_offer(proposer_id='P02', recipient_id='P01',
                         give=Bundle(food=5), receive=Bundle(water=4))
    own_ad = f.make_advertisement(station_id='P01')
    peer_ad = f.make_advertisement(advertisement_id='peer', station_id='P02',
        selling=frozenset({Resource.FOOD}), seeking=frozenset({Resource.WATER}))
    decision, _ = decide(f.make_snapshot(me=f.make_station(inventory=Bundle(60, 60, 60)),
        offers=(offer,), advertisements=(own_ad, peer_ad)), PolicyMemory())
    assert decision.actions[0] == AcceptAction(offer.offer_id)
    assert WithdrawAction(own_ad.advertisement_id) in decision.actions
    assert not any(isinstance(a, AdvertiseAction) for a in decision.actions)
    assert any(isinstance(a, OfferAction) and a.receive.food for a in decision.actions)


def test_uncommitted_stock_controls_return_to_normal_trading():
    tracker = CommitmentTracker()
    tracker.register_inflight('pending', Bundle(water=10))
    offer = f.make_offer(proposer_id='P02', recipient_id='P01',
                         give=Bundle(food=4), receive=Bundle(water=4))
    decision, _ = decide(f.make_snapshot(me=f.make_station(inventory=Bundle(40, 40, 40)),
        offers=(offer,)), PolicyMemory(), tracker)
    assert AcceptAction(offer.offer_id) in decision.actions
    assert any(isinstance(a, AdvertiseAction) for a in decision.actions)
