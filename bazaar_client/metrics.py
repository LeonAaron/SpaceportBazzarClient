"""Responsiveness: where the time goes between a state arriving and its answer.

Four separately measured intervals, all in milliseconds:

  queue     state received by the socket reader -> the loop starts deciding on it
  decide    the strategy's own computation
  response  command sent -> the server's result (or protocol error) arrives
  confirm   result arrives -> a state containing that result arrives

A command *misses its deadline* when the server processes it in a later tick
than the state it was decided from: it acted on a world that had moved on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

SERIES = ("queue", "decide", "response", "confirm")


@dataclass(frozen=True, slots=True)
class SeriesSummary:
    count: int
    p50: float
    p95: float
    max: float
    mean: float

    def as_dict(self) -> dict[str, float]:
        return {"count": self.count, "p50": self.p50, "p95": self.p95,
                "max": self.max, "mean": self.mean}


def percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: always a value that was actually observed."""
    if not sorted_values:
        raise ValueError("no samples")
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize(samples: list[float]) -> SeriesSummary | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return SeriesSummary(
        count=len(ordered),
        p50=round(percentile(ordered, 0.50), 3),
        p95=round(percentile(ordered, 0.95), 3),
        max=round(ordered[-1], 3),
        mean=round(sum(ordered) / len(ordered), 3),
    )


@dataclass
class LatencyRecorder:
    samples: dict[str, list[float]] = field(default_factory=dict)
    deadline_checks: int = 0
    missed_deadlines: int = 0

    def record(self, series: str, milliseconds: float) -> None:
        self.samples.setdefault(series, []).append(milliseconds)

    def note_deadline(self, missed: bool) -> None:
        self.deadline_checks += 1
        if missed:
            self.missed_deadlines += 1

    def summary(self) -> dict[str, SeriesSummary]:
        out = {}
        for name in (*SERIES, *sorted(set(self.samples) - set(SERIES))):
            stats = summarize(self.samples.get(name, []))
            if stats is not None:
                out[name] = stats
        return out

    def report_lines(self) -> list[str]:
        lines = [
            f"{name:8} n={s.count:<4} p50={s.p50:.1f}ms p95={s.p95:.1f}ms max={s.max:.1f}ms"
            for name, s in self.summary().items()
        ]
        if self.deadline_checks:
            lines.append(
                f"deadlines missed {self.missed_deadlines} of {self.deadline_checks} commands "
                "(processed in a later tick than decided)"
            )
        return lines
