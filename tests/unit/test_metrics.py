"""Latency percentiles, sample counts and missed deadlines."""

from __future__ import annotations

import pytest

from bazaar_client.metrics import LatencyRecorder, percentile, summarize


def test_nearest_rank_percentiles_are_observed_values():
    values = sorted([5.0, 1.0, 3.0, 2.0, 4.0, 100.0, 6.0, 7.0, 8.0, 9.0])

    assert percentile(values, 0.5) == 5.0
    assert percentile(values, 0.95) == 100.0
    assert percentile([42.0], 0.95) == 42.0


def test_percentile_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_a_summary_reports_count_typical_slow_and_worst():
    stats = summarize([1.0, 2.0, 3.0, 4.0, 50.0])

    assert (stats.count, stats.p50, stats.p95, stats.max, stats.mean) == (5, 3.0, 50.0, 50.0, 12.0)
    assert summarize([]) is None


def test_the_recorder_keeps_stages_apart_and_counts_missed_deadlines():
    latency = LatencyRecorder()
    for ms in (1.0, 2.0, 3.0):
        latency.record("decide", ms)
    latency.record("response", 40.0)
    latency.note_deadline(missed=False)
    latency.note_deadline(missed=True)

    summary = latency.summary()
    assert list(summary) == ["decide", "response"]
    assert summary["decide"].p50 == 2.0
    assert (latency.deadline_checks, latency.missed_deadlines) == (2, 1)
    lines = latency.report_lines()
    assert any(line.startswith("decide") and "n=3" in line for line in lines)
    assert lines[-1].startswith("deadlines missed 1 of 2")
