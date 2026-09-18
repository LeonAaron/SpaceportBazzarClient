"""Resource bundle arithmetic and offer-perspective resolution."""

from __future__ import annotations

import pytest

from bazaar_client.domain.types import Bundle, NegativeBundleError, OfferStatus, Resource
from tests.fixtures import factories


def test_addition_is_elementwise():
    assert Bundle(1, 2, 3) + Bundle(10, 20, 30) == Bundle(11, 22, 33)


def test_subtraction_is_elementwise():
    assert Bundle(10, 20, 30) - Bundle(1, 2, 3) == Bundle(9, 18, 27)


def test_subtraction_refuses_to_go_negative():
    """Silent negative stock would corrupt every downstream reserve calculation."""
    with pytest.raises(NegativeBundleError):
        Bundle(1, 1, 1) - Bundle(2, 0, 0)


def test_saturating_subtraction_floors_at_zero():
    assert Bundle(1, 5, 0).saturating_sub(Bundle(3, 2, 4)) == Bundle(0, 3, 0)


def test_negative_construction_is_rejected():
    with pytest.raises(NegativeBundleError):
        Bundle(-1, 0, 0)


def test_single_builds_a_one_resource_bundle_with_explicit_zeros():
    assert Bundle.single(Resource.FOOD, 5) == Bundle(0, 5, 0)


def test_get_reads_by_resource_enum():
    bundle = Bundle(1, 2, 3)
    assert [bundle.get(r) for r in Resource] == [1, 2, 3]


def test_dominates_is_elementwise_coverage():
    assert Bundle(5, 5, 5).dominates(Bundle(5, 4, 0))
    assert not Bundle(5, 5, 5).dominates(Bundle(6, 0, 0))


def test_zero_and_is_zero():
    assert Bundle.zero().is_zero()
    assert not Bundle(0, 0, 1).is_zero()


def test_scale_multiplies_every_resource():
    assert Bundle(1, 2, 0).scale(3) == Bundle(3, 6, 0)


def test_bundle_is_immutable():
    with pytest.raises(Exception):
        Bundle(1, 1, 1).water = 5


def test_the_specification_worked_example():
    """SPECIFICATIONS.md: start (2,0,1), produce 3 water, upkeep 1 each.

    Result is (4,0,0) with one food unit missing -- extra water cannot substitute.
    """
    inventory = Bundle(2, 0, 1)
    after_production = inventory + Bundle(water=3)
    upkeep = Bundle(1, 1, 1)

    after_upkeep = after_production.saturating_sub(upkeep)
    unmet = upkeep.saturating_sub(after_production)

    assert after_upkeep == Bundle(4, 0, 0)
    assert unmet == Bundle(0, 1, 0)
    assert unmet.total() == 1


# --- offer perspective ----------------------------------------------------


def test_offer_terms_resolve_from_each_party_perspective():
    """give/receive are the proposer's view; the recipient's view is the mirror."""
    offer = factories.make_offer(
        proposer_id="P01", recipient_id="P02", give=Bundle(water=6), receive=Bundle(food=5)
    )

    assert offer.what_station_pays("P01") == Bundle(water=6)
    assert offer.what_station_receives("P01") == Bundle(food=5)
    assert offer.what_station_pays("P02") == Bundle(food=5)
    assert offer.what_station_receives("P02") == Bundle(water=6)


def test_offer_perspective_rejects_a_station_that_is_not_a_party():
    offer = factories.make_offer(proposer_id="P01", recipient_id="P02")

    with pytest.raises(ValueError, match="not a party"):
        offer.what_station_pays("P07")


def test_gift_is_an_offer_with_an_all_zero_receive():
    gift = factories.make_offer(
        proposer_id="P02", recipient_id="P01", give=Bundle(components=1), receive=Bundle.zero()
    )

    assert gift.is_gift_to("P01")
    assert not gift.is_gift_to("P02")


def test_a_priced_offer_is_not_a_gift():
    offer = factories.make_offer(
        proposer_id="P02", recipient_id="P01", give=Bundle(components=1), receive=Bundle(water=1)
    )
    assert not offer.is_gift_to("P01")


# --- deadlines are exclusive ---------------------------------------------


def test_expiry_is_exclusive_at_the_boundary_tick():
    """expires_tick 12 means usable at 11 and unusable from 12 onward."""
    offer = factories.make_offer(expires_tick=12)

    assert not offer.is_expired_at(11)
    assert offer.is_expired_at(12)
    assert offer.is_expired_at(13)


def test_open_offer_is_not_open_once_expired():
    offer = factories.make_offer(status=OfferStatus.OPEN, expires_tick=12)

    assert offer.is_open_at(11)
    assert not offer.is_open_at(12)


def test_non_open_status_is_never_open_even_before_expiry():
    offer = factories.make_offer(status=OfferStatus.ACCEPTED, expires_tick=12)
    assert not offer.is_open_at(5)


def test_advertisement_expiry_is_exclusive():
    ad = factories.make_advertisement(expires_tick=6)
    assert not ad.is_expired_at(5)
    assert ad.is_expired_at(6)


# --- snapshot views -------------------------------------------------------


def test_snapshot_separates_incoming_from_outgoing_offers():
    """Only the recipient may accept and only the creator may withdraw."""
    mine = factories.make_offer(offer_id="mine", proposer_id="P01", recipient_id="P02")
    theirs = factories.make_offer(offer_id="theirs", proposer_id="P02", recipient_id="P01")
    snapshot = factories.make_snapshot(
        self_station_id="P01", tick=0, offers=(mine, theirs)
    )

    assert [o.offer_id for o in snapshot.incoming_open_offers()] == ["theirs"]
    assert [o.offer_id for o in snapshot.outgoing_open_offers()] == ["mine"]


def test_snapshot_views_exclude_expired_offers():
    expired = factories.make_offer(
        offer_id="old", proposer_id="P02", recipient_id="P01", expires_tick=3
    )
    snapshot = factories.make_snapshot(self_station_id="P01", tick=3, offers=(expired,))

    assert snapshot.incoming_open_offers() == ()


def test_own_advertisement_finds_only_our_active_listing():
    ours = factories.make_advertisement(advertisement_id="ours", station_id="P01")
    theirs = factories.make_advertisement(advertisement_id="theirs", station_id="P02")
    snapshot = factories.make_snapshot(
        self_station_id="P01", advertisements=(theirs, ours)
    )

    assert snapshot.own_advertisement().advertisement_id == "ours"


def test_own_advertisement_is_none_when_we_have_no_active_listing():
    theirs = factories.make_advertisement(station_id="P02")
    snapshot = factories.make_snapshot(self_station_id="P01", advertisements=(theirs,))

    assert snapshot.own_advertisement() is None
