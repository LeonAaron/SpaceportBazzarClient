"""Every example situation in scenarios/ gets the decision its file expects.

Each file is a planet state plus the market, with no server and no socket;
adding a scenario file adds a test.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "decide_once.py"
spec = importlib.util.spec_from_file_location("decide_once", SCRIPT)
decide_once = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decide_once)

SCENARIOS = sorted((REPO / "scenarios").glob("*.json"))


def test_there_are_scenarios_to_check():
    assert SCENARIOS


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_the_default_strategy_decides_as_each_scenario_expects(path):
    _, _, problems = decide_once.evaluate(json.loads(path.read_text()))

    assert problems == []


def test_the_check_tells_strategies_apart():
    """A passive planet does not accept even a gift, and the check says so."""
    document = json.loads((REPO / "scenarios" / "incoming-gift.json").read_text())

    _, _, problems = decide_once.evaluate(document, "passive")

    assert problems == ["expected to accept ['offer-7'], accepted []"]


def test_the_command_reports_and_exits_by_the_expectation(capsys):
    assert decide_once.main([str(REPO / "scenarios" / "unfair-offer.json")]) == 0
    out = capsys.readouterr().out
    assert "passed offer-21: asks more than it gives" in out and "as expected" in out
