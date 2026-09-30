"""Our server with several separate client processes, as in a class run."""

from __future__ import annotations

import json

import pytest

from bazaar_sim import orchestrate


def test_strategies_are_assigned_to_every_planet():
    assert orchestrate.assign_strategies("reserve-trader", 3) == ["reserve-trader"] * 3
    assert orchestrate.assign_strategies("reserve-trader:2,passive:1", 3) == [
        "reserve-trader", "reserve-trader", "passive"]


@pytest.mark.parametrize("spec", ["reserve-trader:2", "nonsense:3"])
def test_a_mix_that_does_not_fit_the_world_is_refused(spec):
    with pytest.raises(ValueError):
        orchestrate.assign_strategies(spec, 3)


def test_clients_do_not_inherit_a_stray_token(monkeypatch):
    monkeypatch.setenv("BAZAAR_TOKEN", "someone-elses-key")

    assert "BAZAAR_TOKEN" not in orchestrate.client_environment()


def test_two_client_processes_trade_through_our_server_and_both_survive(tmp_path, capsys):
    """Separate processes, real sockets, the real CLI: the whole path end to end.

    Starting with 5 of everything, water and food run out within a few ticks
    unless the two traders exchange them, so trading is necessary, not optional.
    """
    code = orchestrate.main(["--planets", "3", "--strategies", "reserve-trader:2,passive:1",
                             "--ticks", "12", "--tick-ms", "150", "--starting-stock", "5",
                             "--grace", "40", "--out", str(tmp_path)])

    summary = json.loads((tmp_path / "orchestration.json").read_text())
    report = summary["server_report"]
    assert code == 0, capsys.readouterr().out
    assert report["phase"] == "FINISHED" and report["tick"] == 12
    assert summary["client_exit_codes"] == {"P01": 0, "P02": 0, "P03": 0}
    assert report["transactions"] > 0
    planets = {p["station_id"]: p for p in report["planets"]}
    assert planets["P01"]["imported"]["food"] > 0 and planets["P02"]["imported"]["water"] > 0
    assert not planets["P01"]["failed"] and not planets["P02"]["failed"]
    assert planets["P03"]["exported"] == {"water": 0, "food": 0, "components": 0}
    for sid in ("P01", "P02", "P03"):
        assert (tmp_path / f"{sid}-evidence.jsonl").stat().st_size > 0
        assert (tmp_path / f"{sid}-summary.md").exists()
