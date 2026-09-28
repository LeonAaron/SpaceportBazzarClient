# Architecture

## The shape of the problem

The planet produces one resource and consumes all three. Health falls when
upkeep goes unmet and zero health is permanent, so the client's first job is
never running out, and its second is turning surplus into what it cannot make.

Two facts drive most of the design:

- **The server holds no escrow.** Posting an offer checks that we *could* pay
  but reserves nothing. Several open offers can promise the same stock, so the
  client tracks its own commitments or it will overpromise.
- **Other planets are opaque.** Inventories, health and specialties are never
  disclosed. Everything we believe about a counterparty comes from their public
  advertisements and from trades we were part of.

## Layers

| Layer | Modules | Responsibility |
|---|---|---|
| Connection & codec | `connection/ws_client.py`, `codec/wire.py` | Sockets, framing, encode/decode |
| State & command tracking | `connection/lifecycle.py`, `connection/requests.py`, `connection/throttle.py`, `app.py` | Handshake, phase gating, request ids, routing answers |
| World model | `world/model.py`, `world/commitments.py`, `world/counterparties.py` | Current view, what stock is spoken for, what peers appear to want |
| Decision policy | `policy/*` | What to trade, with whom, on what terms |
| Execution & evidence | `execution/*` | Turn actions into commands, record what happened |

Only `codec/wire.py` and `domain/mappers.py` import the generated protobuf.
`tests/unit/test_layering.py` enforces that, so the policy cannot quietly grow a
dependency on the wire format or the socket.

### Why the domain model is separate from protobuf

Generated classes describe the wire, not the game. A separate model lets the
policy ask real questions (`offer.what_station_pays("P01")`,
`offer.is_gift_to(...)`, `offer.is_expired_at(tick)`) instead of re-deriving
perspective and deadline rules at each call site. Two specific traps are handled
once, in `domain/types.py`, rather than everywhere:

- `give`/`receive` are always the **proposer's** perspective. The resolver
  methods make it impossible to read an incoming offer backwards.
- Deadlines are **exclusive**: `expires_tick` 12 means unusable *from* 12.

## The life of an incoming offer

P02 offers us 3 food for 3 water. Every hop, with where it happens and what it
leaves in the evidence log:

1. **Bytes arrive.** `BazaarConnection.recv_loop`
   ([ws_client.py:79](bazaar_client/connection/ws_client.py#L79)) decodes the
   binary frame into a `Snapshot` and queues it. It never calls decision code,
   so reading continues whatever the strategy is doing.
2. **The session takes it.** `BazaarSession._consume_events`
   ([app.py:189](bazaar_client/app.py#L189)) runs it past the lifecycle state
   machine (stale sequence numbers and wrong runs are rejected here), and
   `_on_snapshot` ([app.py:224](bazaar_client/app.py#L224)) makes it the
   current view and timestamps its arrival.
3. **The loop picks it up.** `TradingLoop.run`
   ([autonomous.py:132](bazaar_client/autonomous.py#L132)) wakes, updates the
   status (`participating`), and calls `step`
   ([autonomous.py:200](bazaar_client/autonomous.py#L200)), which measures how
   long the state waited (`timing.queue_ms`).
4. **The decision.** The configured strategy decides on a worker thread, so
   new states keep arriving meanwhile. For `reserve-trader` that is `decide`,
   whose `_accept_incoming` ([decide.py:146](bazaar_client/policy/decide.py#L146))
   asks `evaluate_incoming` ([accept.py:25](bazaar_client/policy/accept.py#L25)):
   we pay only in our specialty, never more than we get, only for an import,
   and only from stock above our reserve. This offer passes, so the decision is
   `AcceptAction(offer_id)` with reason "our specialty for what we import, at
   least 1:1". Had it failed, `ReserveTrader.explain_passes` would record why.
5. **The record.** `_log_decision`
   ([autonomous.py:273](bazaar_client/autonomous.py#L273)) writes the
   `decision` record ([evidence.py:168](bazaar_client/execution/evidence.py#L168)):
   `decision_id`, what we held, the offer as `open_offers[]` from our side,
   the verdict and reasons, any `passed_offers`, and the timings.
6. **The command.** `Executor.execute`
   ([executor.py:81](bazaar_client/execution/executor.py#L81)) builds the
   message (`build_accept`, [mappers.py:515](bazaar_client/domain/mappers.py#L515))
   and `BazaarSession.send_command` ([app.py:395](bazaar_client/app.py#L395))
   checks readiness, phase and the tick's quota before the bytes leave.
7. **The answer.** The `result` resolves the waiting command in `_on_result`
   ([app.py:256](bazaar_client/app.py#L256)); the executor then waits for a
   state that contains that exact result (`wait_for_result_snapshot`,
   [app.py:340](bazaar_client/app.py#L340)). The command record gets the
   `decision_id`, `request_id`, result code, `transaction_id`, `response_ms`,
   `confirm_ms`, the inventory after, and `deadline_missed` (processed in a
   later tick than decided).
8. **The trade.** The next `decision` record lists it under `new_transactions`.

`python scripts/analyze_evidence.py <log> --offer <offer_id>` prints exactly
this chain for any offer in a real log.

## Strategies are pluggable

The trading loop, the evidence log and the simulator see only the `Strategy`
interface ([strategy.py](bazaar_client/strategy.py)): `decide(snapshot,
memory, commitments, command_budget)` and `explain_passes(snapshot, decision)`.
`--strategy` picks one by name at startup (`reserve-trader`, the default; or
`passive`, the never-trading baseline). Transport (`connection/`) and logging
(`execution/evidence.py`) sit on the other side of that interface, so either
can change without touching a strategy; `tests/unit/test_layering.py` keeps
the policy free of protobuf and sockets and the client free of the simulator.

## The decision API

```python
decide(observation: Snapshot, memory: PolicyMemory, commitments, *, command_budget=None) -> (Decision, PolicyMemory)
```

A pure function: no socket, no protobuf, no clock. Every situation in
`tests/unit/test_decide_compose.py` and `tests/survival/` is a hand-built
snapshot. `Decision` carries the chosen actions *and* the figures behind them
(reserve, available, import targets, spendable specialty, urgency) plus a reason
per action, so a log can explain a choice after the fact.

### Worked example: running low on an import

We produce water, hold `(40, 2, 30)` at tick 0 of a 120-tick run, and P02
advertises selling food. Each import's target is 60 (the rest of the run, capped
at 60 ticks of upkeep), so we are 58 short of food and 30 short of components.
Water above its reserve of 3 gives 37 spendable.

```python
decide(snapshot, memory).actions
# [AdvertiseAction(selling={WATER}, seeking={FOOD, COMPONENTS}, expires_tick=12),
#  OfferAction("P02", give=Bundle(water=20), receive=Bundle(food=20), expires_tick=5),
#  OfferAction("P02", give=Bundle(water=17), receive=Bundle(components=17), expires_tick=5)]
```

The larger shortfall goes first, at the full trade size of 20. The components
offer gets the 17 water still spendable. Both are one-for-one and paid in water.

### Worked example: a gift arrives

```python
gift = Offer(proposer_id="P02", recipient_id="P01",
             give=Bundle(components=1), receive=Bundle.zero(), ...)
decide(snapshot_with(gift), memory) -> [AcceptAction(gift.offer_id)]
```

A gift is an ordinary offer with an all-zero `receive`, and it still has to be
accepted explicitly. It is always worth accepting: it costs nothing.

## What run 2 taught us

The Directorate's run 2 (nine planets, 120 ticks) ended with seven planets dead
and the galaxy holding over a thousand unused units of **each** resource. It was
a distribution failure, not a scarcity one. We were P01, a components producer.
`scripts/analyze_run.py` reproduces these figures from the run log.

| What P01 did | Result |
|---|---|
| Offers asking for more than they gave ("2 components for 4 water") | 72 sent, 67 expired |
| Late offers overpaying up to 8:1 to a partner that had stopped accepting | all expired |
| Gifted and traded away water, which it cannot produce | water hit zero at tick 70 |
| Trades of 1–5 units, so every tick needed a fresh settlement | 21 of 155 offers accepted |
| Kept offering to a planet whose client never connected | wasted commands |
| **Outcome** | died at tick 83 holding 356 components and no water or food |

The two survivors did the opposite: steady one-for-one offers, and every import
bought with their own specialty. Those offers were also impossible for our
committed code, so a different build had been deployed; the client now logs its
branch and commit at startup.

## The trading policy

Seven rules, each a direct answer to the table above:

1. **Pay only with our specialty.** It regenerates every tick; the other two
   resources arrive only by trade. Imports are never offered, gifted, or paid
   away in an accept.
2. **Strict one-for-one.** Every offer gives exactly as many units as it asks
   for. An incoming offer is accepted only if we receive at least what we pay,
   paid in specialty, for something we import. Gifts to us are always accepted.
   All resources carry the same upkeep, so no unit is worth more than another.
3. **Hold enough imports to outlast the market going quiet.** Each import's
   target is enough upkeep for the rest of the run, at least 20 ticks and at most
   60, never below the reserve. In run 2 most clients stopped trading around tick
   45; a planet then lives exactly as long as its stored imports. We want
   `target − available − already on order`, where "on order" is what our open
   offers ask for plus what we accepted this tick. The cap of 60 was chosen in
   simulation: 20 lost to planets that quit, 80 hoarded supply other planets
   needed and shortened the world's survival.
4. **Trade big, but learn each partner's size.** Offer size is
   `min(20, want, spendable specialty, what this partner can take)`. A partner
   short of stock cannot accept a large request however willing it is, so a
   lapsed offer halves what we next ask that partner for and an accepted one
   doubles it.
5. **Ask likely producers.** Partners are ranked by advertising what we want,
   having handed it to us before, seeking our specialty, recency and reliability.
   Planets with no sign of a running client are skipped; if any planet shows
   evidence of producing what we want, guesses are not tried. A partner is rested
   for six ticks only once even one-unit offers to it keep lapsing — resting a
   partner that is merely short of stock would cut off what may be the last
   supplier. At most two offers per resource are open at once, to different
   partners.
6. **A steady advertisement.** Selling our specialty, seeking both imports —
   true every tick, so it rarely changes and partners can rely on it. Renewed
   before expiry at the longest lifetime the rules allow.
7. **Gifts come only from idle specialty.** Once every import is at target,
   specialty above 40 units (two full-size trades) funds gifts of
   20% of that excess, capped at 10, one per tick, with a per-partner cooldown,
   to planets advertising a need for it. In run 2 we ended holding 356 unused
   components. Since every planet running this policy always seeks its imports,
   this spreads spare stock round-robin rather than singling out a planet in
   distress — health is private, so there is no better public signal.

**Reserve.** Held in ticks of upkeep, read from `upkeep_per_tick` and `rules`,
never hardcoded. The depth adapts to measured trade latency, and deepens when
health is low or our own output has dipped. For our specialty it is the floor
that offers, accepts and gifts never dip below.

**Stock is `available_to_commit`, not inventory.** Inventory minus what open
offers already promise, minus what is in flight. Within one decision, accepts
and new offers share one spending balance; unconfirmed gains do not become
spendable.

**Priority per tick**, spending the budget from
`rules.new_commands_per_station_per_tick` top down:

1. **Accept** worthwhile incoming offers — goods in, with no waiting.
2. **Withdraw** offers promising specialty we now critically need.
3. **Advertise** — only when missing, near expiry, or no longer true.
4. **Offer** — one per need to its best partner, then a second choice.
5. **Gift** — only once every import is at target.

**The trade-off we accepted.** Strict one-for-one never overpays, but it also
cannot buy speed. In an economy that stays short for the whole run — production
barely above upkeep and almost no starting stock — a planet cannot import two
units a tick with one spare unit, and no policy that refuses to overpay can fix
that (`test_a_starved_economy_is_outlasted_without_ever_trading_below_parity`).
The real run is not like that: low phases last 12 ticks, and specialty stock
built up in the 5–6 unit phases pays for imports through them.

## Measured survival

`tests/survival/test_world_survival.py` plays full 120-tick runs under run 2's
conditions against stand-ins for the clients seen in that run
(`bazaar_sim/opponents.py`): one that never connected, the greedy build,
small one-for-one traders, one that quit at tick 45, and one that gave its stock
away. "World score" is planet-ticks lived across all nine planets (at most 1,080).

| Scenario | Result |
|---|---|
| All nine planets run this policy | all survive at full health: 1,080 |
| Run 2's field with the greedy P01 build | 0 survivors: 580 |
| The same field with this policy as P01 | 4 survivors, us included: 761–764 |
| Nobody trades | everyone fails at tick 40: 360 |
| Our planet in each of 9 slots × 3 production phases of the run-2 field | survives all 120 ticks at full health in all 27, and no planet lasts longer or ends healthier |
| Against eight greedy / gifting / quitting clients | ours is the last planet standing (vs quitters by 30+ ticks) |
| Against eight small fair traders | everyone survives |

Every level of adoption beats the run-2 field, and full adoption beats every
mix. The tests were checked to fail when a weaker client stands in for ours, or
when the import target is set back to 20. What they cannot show is how other
teams' real clients will behave; the next class run is the real test.

The broader matched comparison against baselines, across seeds, seats and
world sizes, is in [BENCHMARKS.md](BENCHMARKS.md).

## Observability and responsiveness

| Question | Where the answer is |
|---|---|
| Is it running, connected, authenticated, synchronized, playing? | `status.py`: the status ladder, logged and written as `status` records |
| Is our view stale? | `StatusTracker.check_stale`: RUNNING with no state for three ticks |
| Why did it fail to connect? | `diagnostics.py`: configuration / authentication / protocol / network / application, with a hint and exit code |
| What does the planet look like now? | `status_view.py`: panel every `--status-every` ticks, live file with `--status-file` |
| Why did it act, wait or pass on an offer? | `decision` records: `verdict`, `reasons`, `wait_reason`, `passed_offers` |
| How responsive is it? | `metrics.py`: queue, decide, response and confirm latency (p50/p95/max, counts), missed deadlines; in `run_end` and the report |
| Did it stall? | `ticks_skipped` on a decision; gaps in the decision record in the report |

The strategy runs on a worker thread (`asyncio.to_thread`) in live play, so the
socket reader keeps receiving states while a strategy computes;
`states_during_decision` records how many arrived meanwhile.
`test_states_keep_arriving_while_a_slow_strategy_thinks` shows the difference
against running it on the event loop.

## Execution and confirmation

The autonomous loop requests at most one action at a time, using the remaining
command quota for the current tick. It waits for a snapshot containing that
command's exact result before making another decision from the newest state.
A result alone does not release a successful offer's in-flight commitment or
count an earlier snapshot as evidence of its outcome. Rejections release the
hold; an ambiguous timeout retains it and triggers reconnection. Snapshots can
also recover results whose standalone messages were lost.

The sending boundary enforces readiness, RUNNING phase, permanent failure, run
duration, and the per-tick quota. Multiple snapshots do not replenish that quota;
exact request retries do not spend another new-command slot. Reconnects retain
the quota and policy memory, generate fresh request IDs, and repeat readiness.
A different run ID stops the loop so old policy memory cannot enter a new run.
Old snapshots cannot change the current phase. Offer and advertisement deadlines
are capped by both TTL and the run's duration.

## Testing

| Area | Where |
|---|---|
| Wire format | `tests/wire/` — every message round-trips; zeros, `false`, empty lists and both nullable arms survive |
| Lifecycle | `tests/wire/test_lifecycle.py` — handshake, phase gating, control codes, reconnect |
| State handling | `tests/state_handling/` — duplicate and out-of-order snapshots, commitments, counterparty inference |
| Policy | `tests/unit/test_policy_*.py`, `test_decide_compose.py` — each rule, then the composed decision |
| Survival | `tests/survival/` — shortage, production dips, permanent failure, three- and nine-planet economies, delayed acceptances, temporary outages, and a replay of run 2 |
| World survival | `tests/survival/test_world_survival.py` — full runs against stand-ins for run 2's clients: world score, adoption, and whether our planet outlives every other |
| Trading rules | `tests/unit/test_trading_invariants.py` — every action across a grid of 192 situations is one-for-one, paid only in specialty, and within the reserve |
| Logging and tooling | `tests/unit/test_decision_log.py`, `test_analyze_run.py`, `test_analyze_evidence.py` — evidence records and ids, crash-safe rotation, build id, both analyzers, one offer's story |
| Observability | `test_status.py`, `test_diagnostics.py`, `test_metrics.py`, `test_trading_loop_observability.py`, `test_version_and_view.py` — status ladder and staleness, failure categories, latency, waits and stalls, reading states while deciding, the status panel |
| Strategies | `test_strategy.py`, `test_scenarios.py` — selection by name, pass explanations, scenario files checked without a server |
| Execution safety | `tests/unit/test_trading_safety.py` — send gates, same-tick quotas, delayed/missing results, confirmation, reconnects, and the full trading loop |
| Our server | `tests/sim/test_server.py`, `test_economy_rules.py` — every rule on hand-checked examples, over a real socket with our real session |
| Simulation and benchmarks | `tests/sim/test_world_and_benchmark.py`, `test_orchestrate.py` — balanced production, the success score, reproducible benchmarks, separate client processes trading through our server |
| End to end | `tests/integration/` — the real practice server, ten scripted steps |

**What the practice server can and cannot prove.** It runs one fixed script, so
it validates the wire format, the handshake and the full command set precisely —
and it cannot validate the trading policy at all. Any command other than the
scripted one ends the exercise as `scenario mismatch`. That is expected, not a
defect. The policy is therefore covered by unit tests on synthetic snapshots,
by `tests/survival/`, which runs this same `decide` for every planet in the
economy model of `bazaar_sim/economy.py`, and by our own server
([SIMULATOR.md](SIMULATOR.md)), where separate client processes play full
multi-tick games over the real protocol.

These simulations are models, not the real game: nine-planet scenarios vary
response timing and connectivity, but counterparties still derive their terms
from this policy. Settlement and server limits are simulated. It shows the
policy keeps its planet supplied under scarcity and does not trade itself to
death; it cannot predict how real opponents will behave.

**Coverage** is 97% combined statement/branch coverage over handwritten code
(`bazaar_client` and `bazaar_sim`) from all 691 tests (`make cov`, which
includes the practice-server tests); every module added for observability,
strategies, the server and benchmarks is at 96-100%. The
generated `bazaar_pb2.py` is excluded (its correctness is covered by the
round-trip tests instead). Remaining gaps are transport failure paths in
`ws_client.py`, logging setup, `Secret`'s dunder methods, and the
orchestrator's kill-after-terminate fallback.
`scripts/check.py` fails below 85%.
The live integration tests exercise the real socket and scripted exchange, but
an autonomous classroom run with independently written peers remains necessary.
