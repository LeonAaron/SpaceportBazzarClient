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
(reserve, available, surplus, deficit, urgency) plus a reason per action, so a
log can explain a choice after the fact.

### Worked example: the field manual's shortage

Inventory `(2,0,1)`, production 3 water, upkeep 1 each. After the tick the
planet holds `(4,0,0)` and is one food short — extra water cannot substitute.

```python
urgency = compute_urgency(Bundle(4, 0, 0), upkeep=Bundle(1,1,1), reserve=Bundle(3,3,3))
# FOOD -> CRITICAL, WATER -> NONE, COMPONENTS -> CRITICAL
```

`decide` then advertises food and components as sought, and offers water for
them at a premium, because a critical need is worth settling quickly.

### Worked example: a gift arrives

```python
gift = Offer(proposer_id="P02", recipient_id="P01",
             give=Bundle(components=1), receive=Bundle.zero(), ...)
decide(snapshot_with(gift), memory) -> [AcceptAction(gift.offer_id)]
```

A gift is an ordinary offer with an all-zero `receive`, and it still has to be
accepted explicitly. It is always worth accepting: it costs nothing.

## The trading policy

**Reserve.** Held in ticks of upkeep, read from `upkeep_per_tick` and `rules`,
never hardcoded. The depth adapts to measured trade latency (median round-trip
of our own settled offers), and deepens when health is low or when our own
specialty's output has dipped. Capped so a jittery market cannot make us hoard.

**Stock is `available_to_commit`, not inventory.** Inventory minus what open
offers already promise, minus what is in flight. Within a proposed batch,
acceptance costs and new offers share one spending balance; unconfirmed gains
and withdrawals do not free stock for subsequent actions. Gifts use only the
surplus and offer slots left after those actions.

**Urgency.** `CRITICAL` when uncommitted stock cannot cover the next upkeep,
`WATCH` below reserve, else `NONE`.

**Priority per tick**, spending the budget from
`rules.new_commands_per_station_per_tick` top down:

1. **Accept** worthwhile incoming offers — the only action that brings goods in.
2. **Withdraw** offers promising something now critical. Expiry is free, so a
   command is only spent when the terms became dangerous.
3. **Advertise** — one slot per planet, so it is replaced only on a material
   change, a new critical need, or near expiry.
4. **Offer** — best counterparty per wanted resource, scored on what they
   advertise selling and seeking, ad recency, and how reliably they have
   accepted before. Never duplicates a pairing we already have open.
5. **Gift** — only when nothing of ours is urgent.

**Pricing.** No currency, so price is the ratio between bundles. All three
resources carry equal upkeep, so 1:1 is the neutral baseline; urgency sweetens
our side up to a hard cap. Rounding is half-up and the cap is enforced on the
result, because ceiling a one-unit trade would silently pay a 100% premium for
a need that was not urgent.

**Buffer target.** Our specialty is the only resource we generate; the other two
can only arrive by trade. So surplus is converted toward twice the reserve
*before* a shortage bites. Waiting for a deficit means starting negotiations
with nothing left to trade — in simulation that alone was the difference between
surviving and starving.

**Altruism, and why it is not sentiment.** The class win condition is binary and
collective: one planet at zero fails it for everyone, permanently. The prizes
for prosperity are graded and personal. Trading a bounded, recoverable amount of
idle surplus against a discrete, irreversible, shared failure is simply a good
price. A planet kept alive also stays a trading partner. The caps make the
downside bounded: only when nothing of ours is urgent, only from surplus above
reserve, at most 20% of it, one gift per tick, with a per-station cooldown.

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
| Survival | `tests/survival/` — shortage, production dips, permanent failure, three- and nine-planet economies, delayed acceptances and temporary outages |
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
from all 420 tests, including the practice-server integration tests (`make cov`).
The generated `bazaar_pb2.py` is excluded (its correctness is covered by the round-trip tests instead), and
remaining gaps include transport failure paths and supervisor/CLI branches.
The live integration tests exercise the real socket and scripted exchange, but
an autonomous classroom run with independently written peers remains necessary.
