"""Snapshot handling and commitment tracking against synthetic states."""

from __future__ import annotations

import pytest

from bazaar_client.domain.types import Bundle, OfferStatus, PublicationStatus, Resource
from bazaar_client.world.commitments import CommitmentTracker
from bazaar_client.world.counterparties import CounterpartyModel
from bazaar_client.world.model import WorldModel
from tests.fixtures import factories


# --- replacement, not accumulation ---------------------------------------


def test_a_newer_snapshot_replaces_the_view():
    world = WorldModel()
    world.apply(factories.make_snapshot(snapshot_sequence=1, world_version=2))

    assert world.apply(factories.make_snapshot(snapshot_sequence=2, world_version=3))
    assert world.current.world_version == 3


def test_a_duplicate_snapshot_is_ignored():
    """Seeing the same state twice must not change anything."""
    world = WorldModel()
    snapshot = factories.make_snapshot(snapshot_sequence=4)
    world.apply(snapshot)

    assert not world.apply(snapshot)
    assert world.applied_count == 1
    assert world.ignored_count == 1


def test_an_out_of_order_snapshot_does_not_roll_the_view_back():
    world = WorldModel()
    world.apply(factories.make_snapshot(snapshot_sequence=5, world_version=9))

    assert not world.apply(factories.make_snapshot(snapshot_sequence=3, world_version=4))
    assert world.current.snapshot_sequence == 5
    assert world.current.world_version == 9


def test_a_new_snapshot_at_an_unchanged_world_version_is_still_applied():
    """A sync advances snapshot_sequence while world_version stands still."""
    world = WorldModel()
    world.apply(factories.make_snapshot(snapshot_sequence=8, world_version=9))

    applied = world.apply(
        factories.make_snapshot(
            snapshot_sequence=9,
            world_version=9,
            request_results=(factories.make_result(),),
        )
    )

    assert applied
    assert len(world.current.request_results) == 1


def test_transactions_are_never_re_applied_to_inventory():
    """The reported balance already includes settled trades."""
    world = WorldModel()
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(28, 31, 30)),
        transactions=(factories.make_transaction(),),
    )
    world.apply(snapshot)
    world.apply(snapshot)  # a duplicate delivery

    assert world.current.me.inventory == Bundle(28, 31, 30)


def test_reconnect_resets_the_sequence_tracking():
    world = WorldModel()
    world.apply(factories.make_snapshot(snapshot_sequence=9))

    world.reset_for_new_connection()

    assert world.current is None
    assert world.apply(factories.make_snapshot(snapshot_sequence=1))


def test_reading_before_any_snapshot_is_an_explicit_error():
    with pytest.raises(RuntimeError):
        WorldModel().require()


# --- commitments without escrow ------------------------------------------


def test_open_offers_reserve_stock_in_our_own_ledger():
    """Posting checks ability to pay but locks nothing server-side."""
    tracker = CommitmentTracker()
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(10, 10, 10)),
        offers=(factories.make_offer(proposer_id="P01", give=Bundle(water=6)),),
    )

    assert tracker.available_to_commit(snapshot) == Bundle(4, 10, 10)


def test_several_open_offers_can_promise_the_same_stock():
    """Two offers of six water from ten must not read as affordable."""
    tracker = CommitmentTracker()
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(10, 10, 10)),
        offers=(
            factories.make_offer(offer_id="a", proposer_id="P01", give=Bundle(water=6)),
            factories.make_offer(offer_id="b", proposer_id="P01", give=Bundle(water=6)),
        ),
    )

    assert tracker.available_to_commit(snapshot) == Bundle(0, 10, 10)
    assert not tracker.can_afford(snapshot, Bundle(water=1))


def test_offers_addressed_to_us_do_not_reserve_our_stock():
    tracker = CommitmentTracker()
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(10, 10, 10)),
        offers=(
            factories.make_offer(proposer_id="P02", recipient_id="P01", give=Bundle(water=6)),
        ),
    )

    assert tracker.available_to_commit(snapshot) == Bundle(10, 10, 10)


@pytest.mark.parametrize(
    "status", [OfferStatus.ACCEPTED, OfferStatus.WITHDRAWN, OfferStatus.EXPIRED]
)
def test_a_closed_offer_stops_committing_stock(status):
    tracker = CommitmentTracker()
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(10, 10, 10)),
        offers=(factories.make_offer(proposer_id="P01", give=Bundle(water=6), status=status),),
    )

    assert tracker.available_to_commit(snapshot) == Bundle(10, 10, 10)


def test_an_expired_offer_frees_its_stock():
    """Deadlines are exclusive, so at expires_tick the offer can no longer settle."""
    tracker = CommitmentTracker()
    snapshot = factories.make_snapshot(
        tick=6,
        me=factories.make_station(inventory=Bundle(10, 10, 10)),
        offers=(
            factories.make_offer(proposer_id="P01", give=Bundle(water=6), expires_tick=6),
        ),
    )

    assert tracker.available_to_commit(snapshot) == Bundle(10, 10, 10)


def test_an_in_flight_offer_reserves_before_it_reaches_a_snapshot():
    """Covers the gap between sending an offer and seeing it in the next state."""
    tracker = CommitmentTracker()
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(10, 10, 10))
    )
    tracker.register_inflight("req-1", Bundle(water=4))

    assert tracker.available_to_commit(snapshot) == Bundle(6, 10, 10)


def test_resolving_an_in_flight_offer_hands_authority_to_the_snapshot():
    tracker = CommitmentTracker()
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(10, 10, 10))
    )
    tracker.register_inflight("req-1", Bundle(water=4))
    tracker.resolve_inflight("req-1")

    assert tracker.available_to_commit(snapshot) == Bundle(10, 10, 10)


def test_commitments_never_report_negative_availability():
    tracker = CommitmentTracker()
    snapshot = factories.make_snapshot(
        me=factories.make_station(inventory=Bundle(1, 1, 1)),
        offers=(factories.make_offer(proposer_id="P01", give=Bundle(water=5)),),
    )

    assert tracker.available_to_commit(snapshot) == Bundle(0, 1, 1)


# --- counterparty inference ----------------------------------------------


def test_counterparties_are_learned_from_public_advertisements():
    model = CounterpartyModel()
    model.update(
        factories.make_snapshot(
            advertisements=(
                factories.make_advertisement(
                    station_id="P02",
                    selling=frozenset({Resource.FOOD}),
                    seeking=frozenset({Resource.WATER}),
                ),
            )
        )
    )

    peer = model.get("P02")
    assert peer.selling == frozenset({Resource.FOOD})
    assert peer.seeking == frozenset({Resource.WATER})
    assert peer.display_name == "Verdant"


def test_we_are_not_our_own_counterparty():
    model = CounterpartyModel()
    model.update(factories.make_snapshot())

    assert model.get("P01") is None


def test_a_repeated_need_builds_a_streak():
    """A need repeated across ticks is the only public signal of distress."""
    model = CounterpartyModel()
    for tick in range(3):
        model.update(
            factories.make_snapshot(
                tick=tick,
                advertisements=(
                    factories.make_advertisement(
                        station_id="P02",
                        seeking=frozenset({Resource.WATER}),
                        expires_tick=99,
                    ),
                ),
            )
        )

    assert model.get("P02").seeking_streak[Resource.WATER] == 3


def test_several_snapshots_within_one_tick_count_as_one_observation():
    """The server emits a snapshot per world change, so a tick can carry many."""
    model = CounterpartyModel()
    for sequence in range(1, 7):
        model.update(
            factories.make_snapshot(
                tick=0,
                snapshot_sequence=sequence,
                advertisements=(
                    factories.make_advertisement(
                        station_id="P02",
                        seeking=frozenset({Resource.WATER}),
                        expires_tick=99,
                    ),
                ),
            )
        )

    assert model.get("P02").seeking_streak[Resource.WATER] == 1


def test_a_dropped_need_resets_its_streak():
    model = CounterpartyModel()
    model.update(
        factories.make_snapshot(
            advertisements=(
                factories.make_advertisement(
                    station_id="P02", seeking=frozenset({Resource.WATER}), expires_tick=99
                ),
            )
        )
    )
    model.update(
        factories.make_snapshot(
            advertisements=(
                factories.make_advertisement(
                    station_id="P02", seeking=frozenset({Resource.FOOD}), expires_tick=99
                ),
            )
        )
    )

    assert Resource.WATER not in model.get("P02").seeking_streak


def test_a_replaced_advertisement_is_not_treated_as_current():
    model = CounterpartyModel()
    model.update(
        factories.make_snapshot(
            advertisements=(
                factories.make_advertisement(
                    station_id="P02",
                    selling=frozenset({Resource.FOOD}),
                    status=PublicationStatus.REPLACED,
                ),
            )
        )
    )

    assert model.get("P02").selling == frozenset()


def test_acceptance_history_feeds_a_reliability_rate():
    model = CounterpartyModel()
    assert model.get("P02") is None

    model.update(
        factories.make_snapshot(
            offers=(
                factories.make_offer(
                    offer_id="a", proposer_id="P01", recipient_id="P02",
                    status=OfferStatus.ACCEPTED,
                ),
                factories.make_offer(
                    offer_id="b", proposer_id="P01", recipient_id="P02",
                    status=OfferStatus.EXPIRED,
                ),
            )
        )
    )

    peer = model.get("P02")
    assert (peer.offers_sent_to, peer.offers_accepted_by) == (2, 1)
    assert peer.accept_rate == 0.5


@pytest.mark.parametrize("status", [OfferStatus.WITHDRAWN, OfferStatus.RUN_ENDED])
def test_our_own_withdrawal_does_not_count_against_the_recipient(status):
    """Withdrawing is our choice and a run ending is nobody's, so neither may
    drag down how reliable a station looks."""
    model = CounterpartyModel()
    model.update(
        factories.make_snapshot(
            offers=(
                factories.make_offer(
                    proposer_id="P01", recipient_id="P02", status=status
                ),
            )
        )
    )

    peer = model.get("P02")
    assert peer.offers_sent_to == 0
    assert peer.accept_rate == 0.5


def test_offer_history_counts_each_offer_once():
    model = CounterpartyModel()
    snapshot = factories.make_snapshot(
        offers=(
            factories.make_offer(
                offer_id="a", proposer_id="P01", recipient_id="P02",
                status=OfferStatus.ACCEPTED,
            ),
        )
    )
    model.update(snapshot)
    model.update(snapshot)

    assert model.get("P02").offers_sent_to == 1


def test_a_failed_station_is_excluded_from_candidates():
    """Permanent failure removes trading eligibility for the rest of the run."""
    model = CounterpartyModel()
    model.update(factories.make_snapshot())
    model.mark_failed("P02")

    assert model.get("P02").station_failed
    assert "P02" not in [s.station_id for s in model.stations()]
