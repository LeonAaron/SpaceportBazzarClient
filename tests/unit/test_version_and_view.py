"""Build identity for reproducibility, and the live status panel."""

from __future__ import annotations

from bazaar_client.cli import main
from bazaar_client.domain.types import Bundle
from bazaar_client.policy.decide import decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.status_view import render_status, write_status_file
from bazaar_client.version import build_info, describe_build, redact_argv
from tests.fixtures import factories


def test_tokens_never_reach_a_recorded_command_line():
    assert redact_argv(["--mode", "trade", "--token", "s3cret", "--token=also-secret"]) == [
        "--mode", "trade", "--token", "***", "--token=***"]


def test_build_info_names_the_code_and_interpreter():
    info = build_info()

    assert {"build", "dirty", "python", "platform", "hash_seed", "argv"} <= set(info)
    assert describe_build(dict(info, build="main@abc", dirty=True)) == "main@abc (with uncommitted changes)"
    assert describe_build(dict(info, build="main@abc", dirty=None)) == "main@abc (tree state unknown)"


def test_version_prints_the_build_without_needing_a_token(capsys, monkeypatch):
    monkeypatch.delenv("BAZAAR_TOKEN", raising=False)

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("bazaar-client ")


def panel_snapshot():
    ours = factories.make_offer(offer_id="offer-12", proposer_id="P01", recipient_id="P02",
                                give=Bundle(water=20), receive=Bundle(food=20), expires_tick=45)
    theirs = factories.make_offer(offer_id="offer-19", proposer_id="P02", recipient_id="P01",
                                  give=Bundle(food=3), receive=Bundle.zero(), expires_tick=44)
    trade = factories.make_transaction(transaction_id="txn-9", proposer_id="P01", recipient_id="P02",
                                       give=Bundle(water=5), receive=Bundle(food=5), settled_tick=41)
    return factories.make_snapshot(tick=42, offers=(ours, theirs), transactions=(trade,))


def test_the_panel_shows_reserves_pending_offers_and_recent_trades():
    snapshot = panel_snapshot()
    decision, _ = decide(snapshot, PolicyMemory())

    panel = render_status(snapshot, decision, status="participating", strategy="reserve-trader",
                          in_flight=1)

    first, reserves, pending, *rest = panel.splitlines()
    assert "tick 42/120" in first and "participating" in first and "health 100/100" in first
    assert reserves.startswith("reserves") and "food 30/" in reserves and "target" in reserves
    assert pending.startswith("pending") and "accept offer_id=offer-19" in pending
    assert "1 command(s) awaiting confirmation" in pending
    body = "\n".join(rest)
    assert "out offer-12 -> P02  pay 20 water  get 20 food  expires t45" in body
    assert "in  offer-19 <- P02  pay 0  get 3 food" in body
    assert "t41 txn-9  P02  paid 5 water  got 5 food" in body


def test_a_quiet_market_is_shown_as_such():
    panel = render_status(factories.make_snapshot())

    assert "open offers   none" in panel and "recent trades none yet" in panel
    assert "pending       waiting" in panel


def test_the_status_file_is_plain_text_or_a_self_refreshing_page(tmp_path):
    write_status_file(tmp_path / "status.txt", "line <1>")
    write_status_file(tmp_path / "status.html", "line <1>")

    assert (tmp_path / "status.txt").read_text() == "line <1>\n"
    page = (tmp_path / "status.html").read_text()
    assert "http-equiv='refresh'" in page and "line &lt;1&gt;" in page
    assert not list(tmp_path.glob("*.tmp"))
