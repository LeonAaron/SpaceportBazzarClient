# Comparing strategies

```sh
python -m bazaar_sim.benchmark                               # writes logs/benchmark.json and .md
python -m bazaar_sim.benchmark --check logs/benchmark.json    # re-run every row, compare exactly
```

## Success, defined before comparing

A run is ranked by these criteria, in this order (`bazaar_sim/world.py`,
`SUCCESS_DEFINITION` and `Score.rank_key`):

1. **Collective success**: every planet reaches the end of the run.
2. **Survivors**: how many planets are alive at the end.
3. **World alive ticks**: planet-ticks lived across the whole world.
4. **Shortage ticks**: fewer is better, summed over every planet.
5. **The planet under test**: whether it survived, then its health.

Our own stockpile is deliberately not a criterion. In the class game one planet
failing fails everyone, so a strategy that hoards while neighbours starve loses.

## Matched cases

Every candidate plays exactly the same cases, so a difference in score is the
strategy's doing:

| Dimension | Values (default) | What it varies |
|---|---|---|
| Scenario | `run2-field`, `fair-field`, `quitters`, `self-play-balanced` | who the other planets are |
| Seed | 1–5 | production phase, each planet's starting stock (24–36), opponents' seating |
| Seat | 0, 1, 2 | which planet we are, and so which resource we produce |
| World size | 3, 5, 9 (self-play) | number of planets, with balanced production |
| Candidate | `reserve-trader`, `passive`, `small-fair` | the strategy under test and two baselines |

- `run2-field`: the eight other clients seen in the Directorate's run 2
  (never connected, greedy, small fair traders, a quitter, a gifter).
- `fair-field`: eight small one-for-one traders.
- `quitters`: eight traders that stop at tick 45.
- `self-play-balanced`: every planet runs the candidate, output exactly
  matching consumption (`balanced_production`, ±1 variation).

Baselines: `passive` never trades (the floor any strategy must beat);
`small-fair` is the simple steady trader the run-2 survivors resembled.

## Reading the output

The markdown report lists, per scenario and candidate: runs, how many ended
with every planet alive, how many our planet survived, failed runs, mean
survivors, mean and worst world alive ticks, mean shortage ticks. Then
head-to-head wins/ties/losses on matched cases, and **every failed run** with
which planets failed and when. Failures are the point: a scenario where no
strategy saves everyone (`quitters`) is reported as such, not dropped.

Results at the time of writing (`logs/benchmark.md`, 180 runs, 120 ticks):

| Scenario | reserve-trader | small-fair | passive |
|---|---|---|---|
| fair-field: all 9 survive | 15/15 | 15/15 | 0/15 |
| self-play-balanced (3/5/9 planets): all survive | 15/15 | 10/15 | 0/15 |
| run2-field: our planet survives | 15/15 | 9/15 | 0/15 |
| run2-field: mean world alive ticks (max 1080) | 778.9 | 760.0 | 620.7 |
| quitters: mean world alive ticks | 695.2 | 632.3 | 589.7 |

Head to head on the 60 matched cases: reserve-trader beats passive 59–1 and
beats small-fair 30 times, ties 25 and loses 5. Every loss is in `run2-field`:
our planet survives at full health, but a small-fair client in our seat leaves
one or two more other planets alive. That is a real finding about the policy,
recorded rather than tuned away here.

## Reproducing a result

Everything that decides a run is recorded: the JSON's `meta` holds the build
(branch@commit and whether the tree had uncommitted changes), Python version,
platform, hash seed and command line; each row holds its scenario, seed, seat,
world size, candidate and ticks. The simulation is deterministic (checked
across hash seeds), so `--check` re-runs every row and reports any difference;
a different build is noted. `scripts/check.py` does this on every CI run.

Live multi-process runs (`bazaar_sim.orchestrate`) depend on scheduling, so
they are not bit-for-bit repeatable. For those, the evidence each client writes
records the order and timing of everything it saw and did (snapshot sequence
numbers, timestamps, how many states arrived during each decision), which is
what an investigation of a surprising run needs.
