"""Rules the policy must never break, checked across a broad grid of situations.

Each example test elsewhere shows one case. These sweep specialties, stock
levels, incoming offers of every shape and a mix of partner advertisements, and
assert on every single action `decide` produces:

  * an offer pays only with our specialty and is exactly one-for-one;
  * a gift is only ever our specialty;
  * an accept never pays more than it receives, nor with anything but our
    specialty.
"""

from __future__ import annotations

import itertools

import pytest

from bazaar_client.domain.types import Bundle, Resource
from bazaar_client.execution.actions import AcceptAction, OfferAction
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from tests.fixtures import factories

STOCK_LEVELS = (0, 4, 25, 80)
RULES = factories.make_rules(new_commands_per_station_per_tick=10, max_open_outgoing_offers=24)


def incoming_offers(specialty: Resource) -> tuple:
    """Offers to us of every shape: gifts, fair, generous, greedy, and wrong-currency."""
    other = next(r for r in Resource if r != specialty)
    third = next(r for r in Resource if r not in (specialty, other))
    shapes = [
        (Bundle.single(other, 3), Bundle.zero()),                  # gift
        (Bundle.single(other, 6), Bundle.single(specialty, 6)),    # fair
        (Bundle.single(other, 8), Bundle.single(specialty, 5)),    # generous
        (Bundle.single(other, 2), Bundle.single(specialty, 7)),    # greedy
        (Bundle.single(other, 4), Bundle.single(third, 4)),        # wants an import
        (Bundle.single(specialty, 4), Bundle.single(specialty, 4)),  # pointless
    ]
    return tuple(
        factories.make_offer(
            offer_id=f"in-{i}", proposer_id=f"P0{2 + i % 3}", recipient_id="P01",
            give=give, receive=receive, expires_tick=50,
        )
        for i, (give, receive) in enumerate(shapes)
    )


def partner_ads() -> tuple:
    return tuple(
        factories.make_advertisement(
            advertisement_id=f"ad-{station}", station_id=station,
            selling=frozenset({resource}), seeking=frozenset(Resource) - {resource},
            expires_tick=99,
        )
        for station, resource in (("P02", Resource.WATER), ("P03", Resource.FOOD),
                                  ("P04", Resource.COMPONENTS))
    )


def situations():
    for specialty, levels in itertools.product(
        Resource, itertools.product(STOCK_LEVELS, repeat=3)
    ):
        yield specialty, Bundle(*levels)


def decisions_for(specialty: Resource, inventory: Bundle):
    offers = incoming_offers(specialty)
    snapshot = factories.make_snapshot(
        rules=RULES,
        me=factories.make_station(specialty=specialty, inventory=inventory),
        offers=offers,
        advertisements=partner_ads(),
        directory=tuple(
            factories.DirectoryEntry(f"P0{i}", f"Planet {i}") for i in range(1, 5)
        ),
    )
    decision, _ = decide(snapshot, PolicyMemory())
    return decision, {o.offer_id: o for o in offers}


@pytest.mark.parametrize("specialty", list(Resource))
def test_offers_are_one_for_one_and_paid_only_in_our_specialty(specialty):
    checked = 0
    for spec, inventory in situations():
        if spec is not specialty:
            continue
        decision, _ = decisions_for(spec, inventory)
        for action in decision.actions:
            if not isinstance(action, OfferAction):
                continue
            checked += 1
            paid = {r for r in Resource if action.give.get(r) > 0}
            assert paid == {specialty}, (inventory, action)
            if action.is_gift:
                continue
            assert action.give.total() == action.receive.total(), (inventory, action)
            assert action.receive.get(specialty) == 0, (inventory, action)
    assert checked > 0


@pytest.mark.parametrize("specialty", list(Resource))
def test_accepts_never_pay_more_than_they_bring_in(specialty):
    checked = 0
    for spec, inventory in situations():
        if spec is not specialty:
            continue
        decision, offers = decisions_for(spec, inventory)
        for action in decision.actions:
            if not isinstance(action, AcceptAction):
                continue
            checked += 1
            offer = offers[action.offer_id]
            cost = offer.what_station_pays("P01")
            gain = offer.what_station_receives("P01")
            assert cost.total() <= gain.total(), (inventory, offer)
            assert {r for r in Resource if cost.get(r) > 0} <= {specialty}, (inventory, offer)
    assert checked > 0


def test_nothing_accepted_or_offered_ever_dips_into_the_specialty_reserve():
    """Posting and accepting lock nothing, so every promise must stay payable."""
    for specialty, inventory in situations():
        decision, offers = decisions_for(specialty, inventory)
        promised = 0
        for action in decision.actions:
            if isinstance(action, OfferAction):
                promised += action.give.get(specialty)
            elif isinstance(action, AcceptAction):
                promised += offers[action.offer_id].what_station_pays("P01").get(specialty)
        assert promised <= max(0, inventory.get(specialty) - decision.reserve.get(specialty)), (
            specialty, inventory, decision.actions
        )
