"""Per-tick command budget.

The limit comes from rules.new_commands_per_station_per_tick on every snapshot,
so a mid-run rules change is picked up rather than baked in.
"""

from __future__ import annotations


class CommandThrottle:
    def __init__(self, limit_per_tick: int) -> None:
        self._limit = limit_per_tick
        self._tick = 0
        self._sent = 0
        self._blocked_until_tick = 0

    @property
    def limit(self) -> int:
        return self._limit

    def update_limit(self, limit_per_tick: int) -> None:
        self._limit = limit_per_tick

    def _roll_to(self, tick: int) -> None:
        if tick != self._tick:
            self._tick = tick
            self._sent = 0

    def remaining(self, tick: int) -> int:
        self._roll_to(tick)
        if tick < self._blocked_until_tick:
            return 0
        return max(0, self._limit - self._sent)

    def record_sent(self, tick: int, count: int = 1) -> None:
        self._roll_to(tick)
        self._sent += count

    def block_until(self, tick: int | None) -> None:
        """Honour RESULT_CODE_RATE_LIMITED's retry_after_tick."""
        if tick is not None:
            self._blocked_until_tick = max(self._blocked_until_tick, tick)

    @property
    def blocked_until_tick(self) -> int:
        return self._blocked_until_tick
