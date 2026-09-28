"""Which decision function the trading loop runs, chosen by name at startup.

Every strategy has the same shape as `policy.decide`: a pure function of one
snapshot plus carried memory, returning the actions and the reasons for them.
The trading loop, the evidence log and the simulator only ever see this
interface, so a strategy can be swapped with `--strategy` and no code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bazaar_client.domain.types import Phase, Snapshot
from bazaar_client.execution.actions import AcceptAction
from bazaar_client.policy.accept import evaluate_incoming
from bazaar_client.policy.decide import Decision, decide
from bazaar_client.policy.memory import PolicyMemory
from bazaar_client.world.commitments import CommitmentTracker

DEFAULT_STRATEGY = "reserve-trader"


class UnknownStrategyError(ValueError):
    """Raised when a strategy name is not registered."""


class Strategy(Protocol):
    name: str
    description: str

    def decide(
        self,
        observation: Snapshot,
        memory: PolicyMemory,
        commitments: CommitmentTracker | None = None,
        *,
        command_budget: int | None = None,
    ) -> tuple[Decision, PolicyMemory]: ...

    def explain_passes(self, observation: Snapshot, decision: Decision) -> dict[str, str]:
        """Why each open offer addressed to us was not accepted, keyed by offer id."""
        ...


def _not_accepted(observation: Snapshot, decision: Decision):
    accepted = {a.offer_id for a in decision.actions if isinstance(a, AcceptAction)}
    return [o for o in observation.incoming_open_offers() if o.offer_id not in accepted]


def _gate_reason(observation: Snapshot, decision: Decision) -> str | None:
    """Set when the policy stopped before looking at any offer."""
    if observation.phase is not Phase.RUNNING:
        return f"not considered: phase is {observation.phase.name}"
    if observation.me.failed_once:
        return "not considered: our station has failed"
    if observation.tick >= observation.rules.duration_ticks:
        return "not considered: run duration reached"
    blocked = next((r for r in decision.reasons if r.startswith("rate limited")), None)
    return f"not considered: {blocked}" if blocked else None


@dataclass(frozen=True)
class ReserveTrader:
    """The trading policy in `bazaar_client.policy` (see ARCHITECTURE.md)."""

    name: str = DEFAULT_STRATEGY
    description: str = "hold import reserves, trade specialty 1:1, gift idle surplus (default)"

    def decide(self, observation, memory, commitments=None, *, command_budget=None):
        return decide(observation, memory, commitments, command_budget=command_budget)

    def explain_passes(self, observation, decision):
        """Re-asks the policy's own accept rule, read-only, for offers it left alone.

        Uses the specialty spendable at the start of the decision; an offer the
        rule would take was left for a later tick because the command budget
        went to higher-priority actions first.
        """
        offers = _not_accepted(observation, decision)
        gate = _gate_reason(observation, decision)
        if gate is not None:
            return {o.offer_id: gate for o in offers}
        reasons = {}
        for offer in offers:
            verdict = evaluate_incoming(
                offer, observation.self_station_id, observation.me.specialty, decision.spendable
            )
            reasons[offer.offer_id] = (
                "worth accepting, but this tick's command budget went elsewhere or ran out"
                if verdict.accept else verdict.reason
            )
        return reasons


@dataclass(frozen=True)
class Passive:
    """Never sends a command. The baseline every other strategy must beat."""

    name: str = "passive"
    description: str = "observe only, never trade: a baseline for comparisons"

    def decide(self, observation, memory, commitments=None, *, command_budget=None):
        decision = Decision(available=observation.me.inventory)
        decision.reasons.append("passive strategy: observing only, no commands by design")
        return decision, memory.observe(observation)

    def explain_passes(self, observation, decision):
        return {o.offer_id: "passive strategy never accepts" for o in _not_accepted(observation, decision)}


STRATEGIES: dict[str, Strategy] = {s.name: s for s in (ReserveTrader(), Passive())}


def get_strategy(name: str) -> Strategy:
    try:
        return STRATEGIES[name]
    except KeyError:
        raise UnknownStrategyError(
            f"unknown strategy {name!r}; choose one of: {', '.join(sorted(STRATEGIES))}"
        ) from None
