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
(`tests/survival/strategies.py`): one that never connected, the greedy build,
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
| Logging and tooling | `tests/unit/test_decision_log.py`, `test_analyze_run.py` — decision records, build id, run-log analyzer |
| Execution safety | `tests/unit/test_trading_safety.py` — send gates, same-tick quotas, delayed/missing results, confirmation, reconnects, and the full trading loop |
| End to end | `tests/integration/` — the real practice server, ten scripted steps |

**What the practice server can and cannot prove.** It runs one fixed script, so
it validates the wire format, the handshake and the full command set precisely —
and it cannot validate the trading policy at all. Any command other than the
scripted one ends the exercise as `scenario mismatch`. That is expected, not a
defect. The policy is therefore covered by unit tests on synthetic snapshots and
by `tests/survival/test_simulated_run.py`, which models the economy the field
manual describes and runs this same `decide` for every planet in it.

These simulations are models, not the real game: nine-planet scenarios vary
response timing and connectivity, but counterparties still derive their terms
from this policy. Settlement and server limits are simulated. It shows the
policy keeps its planet supplied under scarcity and does not trade itself to
death; it cannot predict how real opponents will behave.

**Coverage** is 94% combined statement/branch coverage over handwritten code
from all 491 tests, including the practice-server integration tests (`make cov`).
The generated `bazaar_pb2.py` is excluded (its correctness is covered by the round-trip tests instead), and
remaining gaps include transport failure paths and supervisor/CLI branches.
The live integration tests exercise the real socket and scripted exchange, but
an autonomous classroom run with independently written peers remains necessary.
