SPACEPORT BAZAAR                                      ENGINEERING SELF-ASSESSMENT

How sound is our client?

Assess the current codebase together. Back each rating with evidence.

Engineers: Leon Aaron, Luke                         Date: 9/28/26

Revision / configuration:
Branch `optimized`, strategy `reserve-trader` (trade maximum of resource you
produce to stabilize reserves). `python -m bazaar_client.cli --version` prints
the exact build; `python scripts/check.py` records it with every check result.

0 NOT YET
Absent or not yet understood.

1 IMPLEMENTED
Present, but not yet demonstrated.

2 DEMONSTRATED
Can show repeatable evidence.

Circle or select one rating per row. Add subtotals and the overall total manually.


1  Launch and operate
   Operational readiness

ASSESSMENT QUESTION                                                            RATING

01  Can another pair follow our documentation to install, configure, authenticate, and
    run our client without editing source code?                                   2

02  Can we distinguish "process running," "connected," "authenticated," "state
    synchronized," and "actively participating"? Can we recognize stale state?     2

03  If we cannot connect, does the diagnostic output help us distinguish configuration,
    authentication, protocol, network, and application failures?                  2

Evidence:
- Q01: README.md Setup / Running / Configuration: every setting is a flag or an
  environment variable, nothing compiled in. `--mode check` joins, confirms
  readiness and leaves without trading, so a pair can verify their key safely.
- Q02: bazaar_client/status.py -- starting -> connecting -> connected ->
  authenticated -> synchronized -> waiting | participating -> finished, plus
  stale (RUNNING with no state for 3 ticks) and disconnected. Each change is
  logged and written as a `status` record. "Waiting" (lobby) and "stale"
  (running game gone quiet) are told apart: tests/unit/test_status.py,
  test_trading_loop_observability.py::test_a_running_game_that_goes_silent_is_marked_stale_and_recovers.
- Q03: bazaar_client/diagnostics.py -- every failure is reported as
  `[category] summary -- hint` with exit code 2 configuration, 3 authentication,
  4 protocol, 5 network, 1 application; only network failures are retried.
  README "What a failure means" table. tests/unit/test_diagnostics.py (17
  cases); tests/sim/test_server.py shows a real HTTP 401 diagnosed as
  authentication and a missing subprotocol (HTTP 400) as protocol.

Section subtotal  6 / 6


2  Observe, explain, and review
   Observability, logging, reporting

ASSESSMENT QUESTION

04  Can we see our reserves, current epoch, pending actions, open offers, and recent
    completed trades in an interpretable view?                                    2

05  For a particular offer, can we reconstruct what our client knew, what it decided, why
    it accepted or passed, what it sent, and what the server confirmed?           2

06  Do structured logs preserve that history after a crash or simulation ends, with
    identifiers that connect related events?                                      2

07  Can we generate a useful run summary - resource histories, completed trades,
    rejected requests, disconnected periods, and shortages - without manually reading
    thousands of lines?                                                           2

Evidence:
- Q04: bazaar_client/status_view.py -- a panel with tick/phase/status/health,
  reserves against import targets, pending actions and commands awaiting
  confirmation, open offers in and out, and recent trades; logged every
  `--status-every` ticks and rewritten live with `--status-file status.html`
  (self-refreshing). tests/unit/test_version_and_view.py.
- Q05: `python scripts/analyze_evidence.py <log> --offer offer-37` prints
  knew -> passed/decided (with the reason) -> sent (request id) -> server
  (result, transaction, stock after) -> settled. Passed offers carry the
  policy's own reason (`passed_offers`). tests/unit/test_analyze_evidence.py::
  test_the_story_of_an_incoming_offer_runs_from_knowledge_to_settlement.
- Q06: bazaar_client/execution/evidence.py -- JSONL, each record appended and
  closed immediately; a restart renames the previous log instead of wiping it
  (test_a_restart_keeps_the_previous_runs_evidence). Ids: run_id, session,
  decision_id (decision -> its commands), request_id (command -> result),
  object_id / transaction_id (command -> offer -> trade). run_start / run_end
  frame each process.
- Q07: `python scripts/analyze_evidence.py <log> --html report.html` -- text
  summary and an offline interactive dashboard: health/stock history, completed
  trades, rejections by code, connection periods and downtime, gaps in the
  decision record, shortages, responsiveness, the status history, and a
  searchable stimuli -> decision -> outcome table. Standard library only.

Section subtotal  8 / 8


3  Measure responsiveness
   Performance engineering

ASSESSMENT QUESTION

08  Do we separately measure time waiting to process an event, time making a
    decision, and time awaiting the server's response?                            2

09  Do we record decisions to pass or take no action, as well as actions we send? Can
    we distinguish intentional waiting from a stalled client?                     2

10  Can we report typical and slow responses, sample counts, and missed deadlines?
    Does the client continue receiving updates while a strategy is busy?          2

Evidence:
- Q08: bazaar_client/metrics.py and the trading loop measure four intervals
  separately: queue (state received -> decision starts), decide (strategy
  computing), response (command sent -> result), confirm (result -> confirming
  state). Per decision in `timing`, per command in `response_ms`/`confirm_ms`.
- Q09: every tick with no action still writes a decision with `verdict: wait`
  and a `wait_reason` ("no command slot this tick" / "nothing met the
  strategy's criteria"), plus why each incoming offer was passed. Intentional
  waiting (lobby: status `waiting`) is distinct from a stall: `stale` status,
  `ticks_skipped` on the next decision, and decision-record gaps in the report.
- Q10: run_end and the report give count, p50, p95 and max per stage and
  missed deadlines (a command processed in a later tick than decided). The
  strategy runs on a worker thread in live play; `states_during_decision`
  records states that arrived meanwhile.
  test_trading_loop_observability.py::test_states_keep_arriving_while_a_slow_strategy_thinks
  shows 1 state received during a 300 ms decision on a thread, 0 on the event loop.

Section subtotal  6 / 6


4  Understand and change the design
   Architecture, API design, composability

ASSESSMENT QUESTION

11  Can we choose which strategy to run through a configuration setting or
    command-line argument, without changing source code?                          2

12  Can we give our strategy an example planet state and incoming offer, then
    automatically check its decision without connecting to a server or starting the full
    application?                                                                  2

13  Can we change how messages are sent or how activity is logged without rewriting
    our trading strategy?                                                         2

14  Can each partner explain how an incoming offer moves through the code, where the
    decision happens, and how the response is sent?                               2

Evidence:
- Q11: `--strategy reserve-trader|passive` or BAZAAR_STRATEGY
  (bazaar_client/strategy.py registry); an unknown name is a configuration
  error listing the choices. tests/unit/test_strategy.py.
- Q12: `python scripts/decide_once.py scenarios/unfair-offer.json` -- a JSON
  planet state and incoming offer, the decision and its reasons printed, and
  an `expect` block checked; every file in scenarios/ is a test
  (tests/unit/test_scenarios.py). Plus 136 policy and survival unit tests on hand-built snapshots.
- Q13: strategies see only the `Strategy` interface; transport (connection/)
  and logging (execution/evidence.py) sit behind it. tests/unit/test_layering.py
  keeps protobuf out of the policy, sockets out of policy/world/domain, and the
  simulator out of the client.
- Q14: ARCHITECTURE.md "The life of an incoming offer": eight hops with file and
  line references, from the socket reader to the settled trade, and the
  `--offer` command reproduces that chain from a real log.

Section subtotal  8 / 8

Evidence may include a test, log, report, command, screenshot, or code reference. Both partners should be able to explain it.


5  Test meaningful behavior
   Unit testing and coverage

ASSESSMENT QUESTION

15  Do tests assert expected decisions and state transitions, including boundary cases
    and failure paths?                                                            2

16  What do line and branch coverage reveal about untested code? Can we explain the
    remaining gaps, excluding generated code from our own-code assessment?        2

17  Would our tests fail if we deliberately introduced a plausible bug? Have we turned
    failures from previous simulations into regression tests?                     2

Evidence:
- Q15: 691 tests: policy decisions, lifecycle transitions, status transitions,
  server rules, failure categories, rejected and unanswered commands.
- Q16: 97% combined statement/branch coverage of bazaar_client + bazaar_sim
  (all 691 tests, bazaar_pb2.py excluded); every new module 96-100%. Remaining
  gaps explained in ARCHITECTURE.md "Coverage" (Secret dunders, transport
  failure paths, orchestration kill-after-timeout). check.py fails below 85%.
- Q17: run-2 failures are regression tests (test_run_two_replayed_*, the
  duplicate tick-0 records, reconnect storms); the survival tests were checked
  to fail when a weaker client or the old import target is substituted.
  scenarios/ fail when a strategy that never accepts is swapped in.

Section subtotal  6 / 6


6  Test a functioning trading system
   Integration, simulation, interoperability

ASSESSMENT QUESTION

18  Can we run two clients against our own test server, complete an exchange, and
    verify the resulting inventories?                                             2

19  Can we run a local simulation with multiple copies of our strategy, varying the
    number of planets while keeping resource production and consumption balanced? 2

20  Does our test server implement the documented rules we depend on? Can we
    demonstrate this with small, hand-checked examples and explain what it does not
    simulate?                                                                     2

21  Can our client exchange messages and complete trades with a client developed by
    another pair, using a shared test environment?                                1

Evidence:
- Q18: bazaar_sim/server.py speaks bazaar.protobuf.v2.
  tests/sim/test_server.py::test_two_clients_complete_an_exchange_with_hand_checked_inventories:
  two real client sessions, offer 4 water for 3 food, accept, inventories
  (6,13,10)/(14,7,10), then one tick. tests/sim/test_orchestrate.py runs
  separate client processes through the CLI.
- Q19: `python -m bazaar_sim.orchestrate --planets 5 --ticks 60` (server + N
  client processes). Production is balanced by construction
  (world.balanced_production, checked for 3-9 planets); self-play benchmark at
  3, 5 and 9 planets: every planet survives in 15/15 runs.
- Q20: SIMULATOR.md -- every rule with its hand-checked test (production,
  upkeep, damage, recovery, permanent failure, no escrow, atomic settlement,
  deadlines, limits, ownership, auth, readiness, request-id retry/conflict,
  run end) and a list of what it does not simulate.
- Q21: IMPLEMENTED, not yet demonstrated: our server hosts any pair's client
  (`--open-auth` seats planets in connection order), and our wire format
  passes the Directorate's practice server (63/63 checks). Still needed: one
  session with another pair's client.

Use the provided protocol test server for compatibility checks and your own simulator
for trading behavior. Document the simulator's assumptions and omissions.

Section subtotal  7 / 8


7  Demonstrate improvement
   Benchmarking, reproducibility, continuous verification

ASSESSMENT QUESTION

22  Have we defined success before comparing strategies, including collective survival
    and shortages rather than only our own final stockpile?                       2

23  Have we compared against a simple baseline across multiple matched scenarios,
    assignments, and seeds, reporting failed runs as well as successful ones?     2

24  Can we reproduce an experiment using its code revision, configuration, scenario,
    and recorded events? Where scheduling introduces nondeterminism, have we
    captured enough evidence to investigate it?                                   2

25  Does one command run our checks, and does CI run them on changes? Can we
    identify the exact tested revision we are bringing to class?                  1

Evidence:
- Q22: bazaar_sim/world.py SUCCESS_DEFINITION and BENCHMARKS.md: collective
  success, survivors, world alive ticks, shortage ticks, and only then our own
  planet; our stockpile is deliberately not a criterion.
- Q23: `python -m bazaar_sim.benchmark` -- reserve-trader vs passive and
  small-fair baselines on identical cases: 4 scenarios x 5 seeds x 3 seats and
  3/5/9-planet self-play, 180 runs; every failed run listed. Result:
  59-1 vs passive; 30 wins, 25 ties, 5 losses vs small-fair (all losses in the
  run-2 field; recorded in BENCHMARKS.md).
- Q24: benchmark output records build, dirty flag, Python, platform, hash seed
  and command line; each row its scenario, seed, seat, size and candidate.
  `--check` reproduced 180 of 180 runs exactly. For live runs, the evidence log
  keeps sequence numbers, timestamps and states-during-decision to investigate
  scheduling effects.
- Q25: `python scripts/check.py` (make check) runs compile, tests with coverage,
  the practice server, a live simulation and a reproducibility check, and
  writes logs/check-report.json naming the build and whether the tree was
  clean. .github/workflows/ci.yml runs the same on every push and keeps the
  report. IMPLEMENTED: becomes 2 once pushed and the first CI run passes.

Section subtotal  7 / 8


Overall assessment

Add the seven section subtotals. Maximum: 50 points across 25 questions.

Section maximums  1: /6   2: /8   3: /6   4: /8   5: /6   6: /8   7: /8

TOTAL  48 / 50      (previous: 35 / 50)


Our next two improvements

Choose concrete changes. Name the question number and the evidence you will produce to demonstrate progress.

1. Question(s): 21

   Change and evidence:
   Host a shared session on our server (`python -m bazaar_sim.server --planets 4
   --open-auth`) with at least one other pair's client; keep the server report
   and our evidence log showing a completed trade with their planet.

2. Question(s): 25

   Change and evidence:
   Push the branch so .github/workflows/ci.yml runs; bring the green run and its
   check-report artifact (which names the tested commit) to class, and deploy
   exactly that commit.
