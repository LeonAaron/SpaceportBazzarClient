# Our test server and simulations

The practice server plays one fixed ten-step script, so it proves the wire
format and nothing about trading. `bazaar_sim` fills the gap: one rule engine
(`economy.py`), a server that exposes it over the real protocol (`server.py`),
and an orchestrator that runs the server with several separate client
processes (`orchestrate.py`).

```sh
python -m bazaar_sim.orchestrate --planets 5 --ticks 60 --tick-ms 300
python -m bazaar_sim.server --planets 3 --port 3100          # then connect any client
```

## What it implements

Each rule has a hand-checked test in `tests/sim/test_economy_rules.py` (engine)
or `tests/sim/test_server.py` (over the socket, with our real client session).

| Rule | Behaviour | Test |
|---|---|---|
| Production and upkeep | each tick: + production of the specialty, then 1 of each resource consumed | `test_a_tick_produces_the_specialty_then_consumes_upkeep`; server: `test_two_clients_complete_an_exchange_with_hand_checked_inventories` |
| Shortage damage | 5 health per unit of upkeep short | `test_each_unit_short_costs_five_health_and_a_full_tick_restores_five` |
| Recovery | +5 health per fully supplied tick, capped at 100 | same |
| Permanent failure | zero health is final; the planet's open offers and adverts close; its commands get `STATION_FAILED` | `test_zero_health_is_permanent_and_closes_the_planets_dealings` |
| No escrow | posting an offer reserves nothing; an accept the proposer can no longer pay gets `INSUFFICIENT_RESOURCES` and moves nothing | `test_an_accept_the_proposer_can_no_longer_pay_moves_nothing` |
| Atomic settlement | both bundles move together; imports/exports recorded on both sides | `test_a_settled_trade_records_both_sides_and_its_transaction` |
| One advertisement | a new advertisement replaces the planet's previous one | `test_one_advertisement_per_planet_the_newest_replaces_the_last` |
| Deadlines | `expires_tick` is exclusive; expired offers get `EXPIRED` status and are `NOT_OPEN` | `test_an_offer_expires_at_its_deadline_and_can_no_longer_be_accepted` |
| Validation | TTLs capped by rules and run length; no offers to self, to unknown planets, or giving nothing | `test_offer_arguments_are_validated_before_anything_changes` |
| Limits | open outgoing offers (`LIMIT_REACHED`); commands per tick (`RATE_LIMITED` with `retry_after_tick`) | `test_open_offers_are_capped_per_planet`, `test_the_per_tick_command_limit_is_enforced_with_a_retry_tick` |
| Ownership | only the recipient accepts, only the proposer withdraws | `test_only_the_recipient_accepts_and_only_the_proposer_withdraws` |
| Authentication | a token per planet (401 otherwise), or `--open-auth` seating in connection order | `test_a_wrong_token_is_refused_with_401...`, `test_open_auth_seats_planets_in_connection_order` |
| Subprotocol | `bazaar.protobuf.v2` required (400 otherwise) | `test_a_missing_subprotocol_is_refused_with_400...` |
| Readiness | commands before readiness get `BAD_MESSAGE` with the session kept open | `test_commands_before_readiness_are_refused_but_the_session_stays_open` |
| Request ids | an exact retry returns the stored result; a changed body gets `REQUEST_ID_CONFLICT` | `test_an_exact_retry_returns_the_stored_result_and_a_changed_one_conflicts` |
| Run id / version | wrong run id → `RUN_MISMATCH`, wrong version → `UNSUPPORTED_VERSION`, both closing the session | `test_a_wrong_run_id_closes_the_session`, `test_an_unsupported_protocol_version_closes_the_session` |
| Reconnect | a new connection for a planet fences the old one (`SESSION_FENCED`) | `test_a_new_connection_for_the_same_planet_fences_the_old_one` |
| Run end | phase `FINISHED`, open offers `RUN_ENDED`, `outcome` with `collective_success`, scored report | `test_the_run_finishes_with_an_outcome_and_a_scored_report` |

## Balanced worlds

`--production balanced` (the default) sets each planet's output so that, for
every resource, the planets producing it make exactly what the whole world
consumes: with N planets and upkeep 1, each resource's producers make N units a
tick between them. `--variation k` swings each producer by ±k in staggered
12-tick phases that average to zero, so the balance holds over each 36-tick
cycle. `tests/sim/test_world_and_benchmark.py` checks the balance for 3 to 9
planets. `--production run2` instead replays the class run's 2/5/6 phases.

## What it does not simulate

- **Other teams' real clients.** Opponents are either copies of our strategies
  or the run-2 stand-ins in `opponents.py`. Any other pair's client can connect
  to our server (`--open-auth` removes the key exchange), but only a shared
  session shows how theirs behaves.
- **The real server's timing.** Ticks are a fixed wall-clock interval
  (`--tick-ms`); the real server's scheduling, pauses (`PHASE_PAUSED`) and
  instructor controls are not modelled. The run starts once enough planets are ready.
- **Rejected commands and the quota.** A command refused before processing
  (bad arguments, insufficient stock) does not use one of the tick's command
  slots; the real server may count it.
- **Result storage.** Up to 1000 stored results per planet, where the practice
  server allows 5; `REQUEST_CAPACITY_EXCEEDED` is implemented but rarely reached.
- **History size.** Every offer and trade a planet was party to stays in its
  state for the whole run; the real server may trim history.
- **Advertisement status.** A replaced advertisement disappears rather than
  appearing with status `REPLACED`.
- **Private information.** Like the real server, it never reveals other planets'
  stock, health or specialty; it does not model anything a real server might
  add later.

Where the model and the class server disagree, the class run wins: check the
evidence log and `scripts/analyze_run.py` against the Directorate's log.

## Live browser dashboard

From the project root on your host (with dependencies installed):

```sh
.venv/bin/python -m bazaar_sim.orchestrate --live --planets 6 --ticks 120 --tick-ms 100 --out logs/live-demo
```

The browser opens at `http://127.0.0.1:8766`. Choose a planet to see its current
reason for acting, stock history, pending offers, and completed transfers.
The page stays available after the run; Ctrl+C closes the dashboard. Use a fresh
output directory for each run. `--dashboard-port 8767` selects another port.

To watch a simulation already running, or inspect an existing run folder:

```sh
.venv/bin/python -m bazaar_client.live_dashboard logs/demo-fixed --open
```

This dashboard runs **on the host**, not inside the current Compose container.
It can watch logs that a container writes into the shared project directory.
It also works with trade-mode evidence folders, though those show only the
stations whose logs are present. Hivemind bridges do not currently emit this
evidence format. Terminal logs remain available for debugging.

### Latency and load

The dashboard is an independent, read-only process. It tails only new evidence
bytes every 5 ms and pushes compact state over a local WebSocket at most once
per 16 ms. Browsers paint on animation frames. These are scheduling targets,
not real-time guarantees: OS load, log writing and browser rendering add delay.
The page reports WebSocket delivery time separately from the age of its most
recent update. A quiet or disconnected feed is not evidence of a paused game.

A slow browser has only one pending state; newer states replace it rather than
building a queue or blocking a trader. Histories are bounded to 120 ticks and
recent offers/transfers are bounded. Every event still remains in the evidence
log. The dashboard never sends game commands, so pausing its display does not
pause the simulation. For standalone monitoring, `--poll-ms` and `--frame-ms`
can adjust the latency/CPU tradeoff. Very fast ticks can be skipped visually.

### Markdown summaries and determinism

Each client now writes `P01-summary.md` (and equivalents) after the run.
The summary uses fixed Python aggregations and templates: **identical log
contents and formatter version produce identical Markdown**. No model or
random generation is involved. The Markdown includes outcomes, inventories,
findings, transfers and timing, and labels client observations as such.

Live simulation results themselves can differ because independent client
processes and messages are scheduled differently. The benchmark engine's
seeded, in-process scenarios are the separate deterministic comparison tool.
Archived HTML reports can still be requested explicitly with
`scripts/analyze_evidence.py ... --html report.html`; they are no longer created
automatically. Full log details remain available even when a live frame is skipped.

Local validation of this dashboard: 50 append-to-WebSocket samples measured
1.9 ms median, 6.7 ms p95 and 10.2 ms maximum. These exclude browser painting
and are not performance guarantees. A six-planet, 20-tick run at 150 ms/tick
finished with all clients exiting cleanly; Chromium checks covered selection,
pause/resume, reconnecting, and mobile layout. A longer 50 ms/tick stress run
updated the dashboard through the final server state but exposed trading-client
shutdown/reconnect timeouts. Faster display delivery does not fix those client
lifecycle issues or guarantee that the trading strategy survives a given run.
