"""Whole-world runs: a roster of players on one economy, and how to score them.

Success is defined before any comparison is made; see SUCCESS_DEFINITION and
`Score.rank_key`, which applies it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from bazaar_client.domain.types import Bundle, Resource
from bazaar_sim.economy import DEFAULT_RULES, SimStation, SimulatedEconomy

SUCCESS_DEFINITION = """\
Runs are ranked by these criteria, in this order of priority:

  1. collective success   every planet reaches the end of the run
  2. survivors            how many planets are still alive at the end
  3. world alive ticks    planet-ticks lived across the whole world
  4. shortage ticks       fewer is better, summed over every planet
  5. the subject          whether the planet under test survived, then its health

Our own stockpile is deliberately not a criterion: a planet that hoards while
its neighbours fail has lost the class game."""

RUN_TICKS = 120
PHASES = (2, 5, 6)
PHASE_TICKS = 12

Production = Callable[[int, int], int]
"""(tick, planet index) -> units of specialty produced that tick."""


def run_two_production(phase_offset: int = 0) -> Production:
    """Run 2's schedule: 2/5/6 units in 12-tick phases, staggered by planet."""
    return lambda tick, i: PHASES[(tick // PHASE_TICKS + i + phase_offset) % len(PHASES)]


def specialty_of(index: int) -> Resource:
    return list(Resource)[index % 3]


def balanced_production(planets: int, upkeep: int = 1, variation: int = 0) -> Production:
    """Production that exactly covers the world's consumption of every resource.

    Each resource is consumed `planets * upkeep` times a tick and made only by
    the planets that specialise in it, so their base output splits that need.
    `variation` swings each producer by +/- that many units in staggered
    12-tick phases whose average is zero, so the balance holds over each
    complete 36-tick cycle rather than every single tick.
    """
    if planets < 3:
        raise ValueError("a balanced world needs at least 3 planets, one per resource")
    base: dict[int, int] = {}
    for resource in Resource:
        producers = [i for i in range(planets) if specialty_of(i) is resource]
        share, extra = divmod(planets * upkeep, len(producers))
        for rank, i in enumerate(producers):
            base[i] = share + (1 if rank < extra else 0)
    swing = (-variation, 0, variation)
    return lambda tick, i: max(0, base[i] + swing[(tick // PHASE_TICKS + i) % 3])


@dataclass
class WorldOutcome:
    economy: SimulatedEconomy
    roster: dict[str, object]
    failed_at: dict[str, int]
    ticks: int

    def alive_ticks(self, station_id: str) -> int:
        return self.failed_at.get(station_id, self.ticks)

    @property
    def world_alive_ticks(self) -> int:
        """Planet-ticks lived across the whole world: the collective score."""
        return sum(self.alive_ticks(sid) for sid in self.roster)

    @property
    def survivors(self) -> set[str]:
        return set(self.roster) - set(self.failed_at)

    def station_of(self, player) -> str:
        return next(sid for sid, p in self.roster.items() if p is player)

    def score(self, subject: str | None = None) -> Score:
        return score_economy(self.economy, self.ticks, subject, failed_at=self.failed_at)

    def summary(self) -> str:
        return ", ".join(
            f"{sid}:{self.roster[sid].name}="
            f"{'alive' if sid not in self.failed_at else 't' + str(self.failed_at[sid])}"
            f"/hp{self.economy.stations[sid].health}"
            for sid in self.roster
        )


@dataclass(frozen=True)
class Score:
    planets: int
    collective_success: bool
    survivors: int
    world_alive_ticks: int
    shortage_ticks: int
    unmet_units: int
    subject_survived: bool | None
    subject_health: int | None

    def rank_key(self) -> tuple:
        """Higher is better, in the priority order of SUCCESS_DEFINITION."""
        return (
            self.collective_success,
            self.survivors,
            self.world_alive_ticks,
            -self.shortage_ticks,
            bool(self.subject_survived),
            self.subject_health or 0,
        )

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def score_economy(
    economy: SimulatedEconomy,
    ticks: int,
    subject: str | None = None,
    failed_at: dict[str, int] | None = None,
) -> Score:
    """Score any finished economy, whether run in-process or by the server."""
    if failed_at is None:
        failed_at = {sid: s.first_failure_tick for sid, s in economy.stations.items()
                     if s.failed_once}
    stations = economy.stations
    return Score(
        planets=len(stations),
        collective_success=not failed_at,
        survivors=len(stations) - len(failed_at),
        world_alive_ticks=sum(failed_at.get(sid, ticks) for sid in stations),
        shortage_ticks=sum(s.shortage_ticks for s in stations.values()),
        unmet_units=sum(s.unmet_total.total() for s in stations.values()),
        subject_survived=None if subject is None else subject not in failed_at,
        subject_health=None if subject is None else stations[subject].health,
    )


def run_world(
    players: list,
    ticks: int = RUN_TICKS,
    phase_offset: int = 0,
    *,
    production: Production | None = None,
    starting_stock: list[int] | None = None,
    rules=DEFAULT_RULES,
) -> WorldOutcome:
    """One planet per player, `ticks` long. Defaults reproduce run 2's conditions:
    30 of everything at the start and production cycling 2/5/6 in 12-tick phases."""
    roster = {f"P{i + 1:02}": p for i, p in enumerate(players)}
    stock = starting_stock or [30] * len(roster)
    economy = SimulatedEconomy(
        *(SimStation(sid, specialty_of(i), Bundle(stock[i], stock[i], stock[i]))
          for i, sid in enumerate(roster)),
        rules=rules,
    )
    produce = production or run_two_production(phase_offset)
    failed_at: dict[str, int] = {}
    for tick in range(ticks):
        for i, station in enumerate(economy.stations.values()):
            station.production = produce(tick, i)
        for sid, player in roster.items():
            if economy.stations[sid].failed_once:
                continue
            economy.apply(sid, player.act(economy.observation_for(sid)))
        economy.advance_tick()
        for sid, station in economy.stations.items():
            if station.failed_once and sid not in failed_at:
                failed_at[sid] = economy.tick
    return WorldOutcome(economy, roster, failed_at, ticks)


def seeded_conditions(seed: int, planets: int) -> tuple[int, list[int]]:
    """A seed fixes the production phase offset and each planet's starting stock."""
    rng = random.Random(seed)
    return rng.randrange(len(PHASES)), [rng.randint(24, 36) for _ in range(planets)]
