"""Stand-in clients for the other planets, modelled on the Directorate's run 2.

Each stand-in reproduces a behaviour seen in that run's log (see
`scripts/analyze_run.py`), so "does our policy do better?" is asked against the
opponents we actually met rather than only against copies of ourselves:

  Passive    never trades                                      (run 2: P02 Pelagos)
  Greedy     asks twice what it gives, pays with anything      (run 2: the P01 build)
  SmallFair  steady small one-for-one trades                   (run 2: P06 Nacre, P08 Eos)
  Quitter    a SmallFair client that goes quiet at tick 45     (run 2: P05 Vega)
  Gifter     gives its specialty away every tick               (run 2: P04 Altair)

A player is anything with a `name` and `act(snapshot) -> actions`. Our client's
strategies join through `StrategyPlayer`, which runs the same `Strategy` object
the live trading loop runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bazaar_client.domain.types import Bundle, Resource, Snapshot
from bazaar_client.execution.actions import AcceptAction, AdvertiseAction, OfferAction
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.strategy import DEFAULT_STRATEGY, Strategy, get_strategy
from bazaar_client.world.commitments import CommitmentTracker


def imports_of(obs: Snapshot) -> list[Resource]:
    return [r for r in Resource if r != obs.me.specialty]


def others_of(obs: Snapshot) -> list[str]:
    return [d.station_id for d in obs.directory if d.station_id != obs.self_station_id]


def expiry(obs: Snapshot, ttl: int) -> int:
    return min(obs.tick + ttl, obs.rules.duration_ticks)


def accept_if_not_worse(obs: Snapshot, only_specialty: bool, limit: int = 2) -> list[AcceptAction]:
    """Accept offers that give at least what they cost, while we can pay."""
    accepts, inventory = [], obs.me.inventory
    for offer in obs.incoming_open_offers():
        cost, gain = offer.receive, offer.give
        if gain.total() < cost.total() or not inventory.dominates(cost):
            continue
        if only_specialty and any(cost.get(r) for r in imports_of(obs)):
            continue
        accepts.append(AcceptAction(offer.offer_id))
        inventory = inventory - cost
        if len(accepts) >= limit:
            break
    return accepts


class StrategyPlayer:
    """One of our client's strategies, with the memory it would carry between ticks."""

    def __init__(self, strategy: Strategy | None = None, name: str | None = None) -> None:
        self.strategy = strategy or get_strategy(DEFAULT_STRATEGY)
        self.name = name or self.strategy.name
        self.memory = PolicyMemory()
        self.commitments = CommitmentTracker()

    def act(self, obs: Snapshot) -> list:
        decision, self.memory = self.strategy.decide(obs, self.memory, self.commitments)
        return decision.actions


def OurPolicy() -> StrategyPlayer:
    return StrategyPlayer(get_strategy(DEFAULT_STRATEGY), name="ours")


class Passive:
    name = "passive"

    def act(self, obs: Snapshot) -> list:
        return []


@dataclass
class _RoundRobin:
    position: int = 0

    def next(self, choices: list[str]) -> str:
        choice = choices[self.position % len(choices)]
        self.position += 1
        return choice


@dataclass
class Greedy:
    """Offers 2 of its specialty for 4 of an import, to two planets a tick."""

    name: str = "greedy"
    rotation: _RoundRobin = field(default_factory=_RoundRobin)

    def act(self, obs: Snapshot) -> list:
        actions: list = accept_if_not_worse(obs, only_specialty=False)
        spare = obs.me.inventory.get(obs.me.specialty)
        for want in imports_of(obs):
            if obs.me.inventory.get(want) >= 25 or spare < 2:
                continue
            for _ in range(2):
                if len(actions) >= obs.rules.new_commands_per_station_per_tick or spare < 2:
                    break
                actions.append(OfferAction(
                    self.rotation.next(others_of(obs)),
                    Bundle.single(obs.me.specialty, 2),
                    Bundle.single(want, 4),
                    expiry(obs, 5),
                ))
                spare -= 2
        return actions


@dataclass
class SmallFair:
    """Advertises honestly and offers `size`-for-`size` when an import runs low."""

    size: int = 3
    target: int = 15
    quit_at: int | None = None
    name: str = "small-fair"
    rotation: _RoundRobin = field(default_factory=_RoundRobin)

    def act(self, obs: Snapshot) -> list:
        if self.quit_at is not None and obs.tick >= self.quit_at:
            return []
        actions: list = accept_if_not_worse(obs, only_specialty=True)
        specialty = obs.me.specialty
        if obs.tick % 5 == 0:
            actions.append(AdvertiseAction(
                frozenset({specialty}), frozenset(imports_of(obs)), expiry(obs, 8)
            ))
        spare = obs.me.inventory.get(specialty) - 3
        for want in imports_of(obs):
            if obs.me.inventory.get(want) >= self.target or spare < self.size:
                continue
            if len(actions) >= obs.rules.new_commands_per_station_per_tick:
                break
            actions.append(OfferAction(
                self.rotation.next(others_of(obs)),
                Bundle.single(specialty, self.size),
                Bundle.single(want, self.size),
                expiry(obs, 3),
            ))
            spare -= self.size
        return actions


def Quitter() -> SmallFair:
    return SmallFair(quit_at=45, name="quitter")


@dataclass
class Gifter:
    """Gives two units of its specialty to someone every tick."""

    name: str = "gifter"
    rotation: _RoundRobin = field(default_factory=_RoundRobin)

    def act(self, obs: Snapshot) -> list:
        actions: list = accept_if_not_worse(obs, only_specialty=False)
        if obs.me.inventory.get(obs.me.specialty) > 5:
            actions.append(OfferAction(
                self.rotation.next(others_of(obs)),
                Bundle.single(obs.me.specialty, 2),
                Bundle.zero(),
                expiry(obs, 2),
            ))
        return actions


def run_two_opponents() -> list:
    """The eight other planets of run 2, by behaviour."""
    return [
        Passive(), SmallFair(size=5), Gifter(), Quitter(),
        SmallFair(size=3), SmallFair(size=1), SmallFair(size=1), Greedy(),
    ]


def lineup(subject, opponents: list, slot: int = 0) -> list:
    roster = list(opponents)
    roster.insert(slot, subject)
    return roster
