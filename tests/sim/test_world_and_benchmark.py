"""Balanced worlds, the success definition, and reproducible benchmarks."""

from __future__ import annotations

import json

import pytest

from bazaar_client.domain.types import Resource
from bazaar_sim import benchmark
from bazaar_sim.opponents import OurPolicy, StrategyPlayer
from bazaar_sim.world import (
    PHASE_TICKS,
    Score,
    balanced_production,
    run_world,
    seeded_conditions,
    specialty_of,
)


@pytest.mark.parametrize("planets", range(3, 10))
@pytest.mark.parametrize("variation", [0, 2])
def test_balanced_production_covers_exactly_what_the_world_consumes(planets, variation):
    """Each resource is consumed once per planet per tick; its producers make exactly that."""
    produce = balanced_production(planets, variation=variation)
    cycle = 3 * PHASE_TICKS
    for resource in Resource:
        producers = [i for i in range(planets) if specialty_of(i) is resource]
        made = sum(produce(tick, i) for tick in range(cycle) for i in producers)
        assert made == planets * cycle


def test_a_world_needs_a_producer_for_every_resource():
    with pytest.raises(ValueError):
        balanced_production(2)


@pytest.mark.parametrize("planets", [3, 5, 9])
def test_copies_of_our_strategy_all_survive_a_balanced_world_of_any_size(planets):
    outcome = run_world([OurPolicy() for _ in range(planets)], ticks=60,
                        production=balanced_production(planets, variation=1))

    assert outcome.survivors == set(outcome.roster), outcome.summary()


def test_collective_survival_outranks_everything_else():
    together = Score(9, True, 9, 1080, 50, 60, subject_survived=True, subject_health=40)
    richer_but_alone = Score(9, False, 8, 1079, 0, 0, subject_survived=True, subject_health=100)
    more_alive = Score(9, False, 5, 800, 400, 500, subject_survived=False, subject_health=0)
    fewer_alive = Score(9, False, 4, 900, 100, 100, subject_survived=True, subject_health=100)

    assert together.rank_key() > richer_but_alone.rank_key()
    assert more_alive.rank_key() > fewer_alive.rank_key()


def test_a_seed_fixes_the_conditions_and_different_seeds_differ():
    assert seeded_conditions(7, 9) == seeded_conditions(7, 9)
    assert seeded_conditions(7, 9) != seeded_conditions(8, 9)


def test_every_case_is_played_by_every_candidate_on_identical_terms():
    cases = benchmark.build_cases(["run2-field", benchmark.SELF_PLAY], [1, 2], [0, 4],
                                  ["reserve-trader", "passive"], [3, 5])

    fields = [c for c in cases if c.scenario == "run2-field"]
    assert len(fields) == 2 * 2 * 2
    assert {(c.seed, c.slot) for c in fields if c.candidate == "passive"} == {
        (c.seed, c.slot) for c in fields if c.candidate == "reserve-trader"}
    assert {c.planets for c in cases if c.scenario == benchmark.SELF_PLAY} == {3, 5}


def test_the_same_case_always_produces_the_same_row():
    case = benchmark.Case("quitters", seed=3, slot=2, planets=9, candidate="reserve-trader", ticks=40)
    first, second = benchmark.run_case(case), benchmark.run_case(case)

    assert {k: v for k, v in first.items() if k != "seconds"} == {
        k: v for k, v in second.items() if k != "seconds"}


def test_a_saved_benchmark_reproduces_and_tampering_is_caught(tmp_path):
    cases = benchmark.build_cases(["fair-field"], [1], [0], ["reserve-trader", "passive"], [3], ticks=30)
    result = benchmark.run_benchmark(cases)
    path = tmp_path / "bench.json"
    path.write_text(json.dumps(result))

    count, problems = benchmark.check(path)
    assert count == 2 and not [p for p in problems if not p.startswith("note:")]

    result["rows"][0]["score"]["world_alive_ticks"] += 1
    path.write_text(json.dumps(result))
    _, problems = benchmark.check(path)
    assert any("world_alive_ticks" in p for p in problems)


def test_results_report_failed_runs_and_head_to_head_counts():
    cases = benchmark.build_cases(["fair-field"], [1], [0], ["reserve-trader", "passive"], [3], ticks=60)
    result = benchmark.run_benchmark(cases)
    versus = result["summary"]["versus_baselines"]

    assert {"candidate": "reserve-trader", "baseline": "passive", "wins": 1, "ties": 0,
            "losses": 0} in versus
    report = benchmark.markdown(result)
    assert "## Success, defined before comparing" in report
    assert "Failed runs" in report and result["meta"]["build"] in report


def test_the_benchmark_command_writes_json_and_markdown(tmp_path, capsys):
    out, md = tmp_path / "b.json", tmp_path / "b.md"

    assert benchmark.main(["--scenarios", "fair-field", "--candidates", "reserve-trader",
                           "--seeds", "1", "--slots", "0", "--ticks", "20",
                           "--out", str(out), "--markdown", str(md)]) == 0
    assert json.loads(out.read_text())["meta"]["cases"] == 1
    assert benchmark.main(["--check", str(out)]) == 0
    assert "reproduced 1 of 1 runs exactly" in capsys.readouterr().out


def test_registered_client_strategies_play_in_the_simulator():
    player = StrategyPlayer()

    assert player.name == "reserve-trader"
    assert set(benchmark.CANDIDATES) >= {"reserve-trader", "passive", "small-fair", "greedy"}
