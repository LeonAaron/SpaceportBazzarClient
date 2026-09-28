"""Strategies are chosen by name; each explains the offers it did not take."""

from __future__ import annotations

import pytest

from bazaar_client.cli import main
from bazaar_client.config import ConfigurationError, config_from_args
from bazaar_client.domain.types import Bundle, Phase
from bazaar_client.execution.actions import AcceptAction
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.strategy import (
    DEFAULT_STRATEGY,
    STRATEGIES,
    UnknownStrategyError,
    get_strategy,
)
from tests.fixtures import factories


def incoming(offer_id, give, receive):
    return factories.make_offer(offer_id=offer_id, proposer_id="P02", recipient_id="P01",
                                give=give, receive=receive, expires_tick=50)


def market(*offers, **kwargs):
    return factories.make_snapshot(offers=offers, **kwargs)


def test_the_default_strategy_is_the_trading_policy_unchanged():
    snapshot = market(incoming("gift", Bundle(food=2), Bundle.zero()))
    via_strategy, _ = get_strategy(DEFAULT_STRATEGY).decide(snapshot, PolicyMemory())
    direct, _ = decide(snapshot, PolicyMemory())

    assert via_strategy.actions == direct.actions
    assert via_strategy.reasons == direct.reasons


def test_passive_never_acts_but_still_says_why():
    snapshot = market(incoming("gift", Bundle(food=2), Bundle.zero()))
    decision, memory = get_strategy("passive").decide(snapshot, PolicyMemory())

    assert decision.actions == []
    assert "passive" in decision.reasons[0]
    assert get_strategy("passive").explain_passes(snapshot, decision) == {
        "gift": "passive strategy never accepts"}


def test_an_unknown_strategy_is_named_with_the_choices():
    with pytest.raises(UnknownStrategyError, match="reserve-trader"):
        get_strategy("yolo")


def test_the_strategy_is_chosen_on_the_command_line_or_in_the_environment(monkeypatch):
    assert config_from_args(["--token", "t"]).strategy == DEFAULT_STRATEGY
    assert config_from_args(["--token", "t", "--strategy", "passive"]).strategy == "passive"
    monkeypatch.setenv("BAZAAR_STRATEGY", "passive")
    assert config_from_args(["--token", "t"]).strategy == "passive"


def test_an_unknown_strategy_is_a_configuration_error_with_exit_code_two(capsys):
    with pytest.raises(ConfigurationError):
        config_from_args(["--token", "t", "--strategy", "yolo"])

    assert main(["--token", "t", "--strategy", "yolo"]) == 2
    assert "unknown strategy" in capsys.readouterr().err


def test_every_registered_strategy_has_a_description():
    assert all(strategy.description for strategy in STRATEGIES.values())


# --- why an offer was passed over -----------------------------------------


def test_passes_are_explained_with_the_policys_own_accept_rule():
    greedy = incoming("greedy", Bundle(food=1), Bundle(water=5))
    wrong_currency = incoming("pay-in-food", Bundle(components=3), Bundle(food=3))
    snapshot = market(greedy, wrong_currency)
    strategy = get_strategy(DEFAULT_STRATEGY)
    decision, _ = strategy.decide(snapshot, PolicyMemory())

    passes = strategy.explain_passes(snapshot, decision)

    assert passes == {
        "greedy": "asks more than it gives",
        "pay-in-food": "would pay with a resource we cannot produce",
    }


def test_an_acceptable_offer_left_for_lack_of_command_budget_says_so():
    snapshot = market(incoming("fair", Bundle(food=3), Bundle(water=3)))
    strategy = get_strategy(DEFAULT_STRATEGY)
    decision, _ = strategy.decide(snapshot, PolicyMemory(), command_budget=0)

    assert not any(isinstance(a, AcceptAction) for a in decision.actions)
    assert "command budget" in strategy.explain_passes(snapshot, decision)["fair"]


def test_offers_are_not_considered_outside_a_running_phase():
    snapshot = market(incoming("fair", Bundle(food=3), Bundle(water=3)), phase=Phase.PAUSED)
    strategy = get_strategy(DEFAULT_STRATEGY)
    decision, _ = strategy.decide(snapshot, PolicyMemory())

    assert strategy.explain_passes(snapshot, decision) == {"fair": "not considered: phase is PAUSED"}


def test_accepted_offers_are_not_listed_as_passed():
    snapshot = market(incoming("gift", Bundle(food=2), Bundle.zero()))
    strategy = get_strategy(DEFAULT_STRATEGY)
    decision, _ = strategy.decide(snapshot, PolicyMemory())

    assert strategy.explain_passes(snapshot, decision) == {}
