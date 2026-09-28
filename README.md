# Spaceport Bazaar client

A Python client for the Spaceport Bazaar trading simulation. It connects over
WebSocket, speaks binary Protobuf, keeps its planet supplied, and trades with
the other planets.

- [ARCHITECTURE.md](ARCHITECTURE.md) — design, the trading policy, how an offer
  moves through the code, and what the tests do and do not prove
- [LOG_FORMAT.md](LOG_FORMAT.md) — the structured log format we propose (Task 7), with examples
- [SIMULATOR.md](SIMULATOR.md) — our own test server and local simulations:
  which rules they implement, and what they leave out
- [BENCHMARKS.md](BENCHMARKS.md) — how strategies are compared, and how a result is reproduced
- [DOCKER.md](DOCKER.md) — the container workflow
- [SPECIFICATIONS.md](SPECIFICATIONS.md) — the assignment
- [SweSpec.md](SweSpec.md) — our engineering self-assessment, with the evidence for each rating

**One command checks everything** (tests, coverage, practice server, a live
simulation, benchmark reproducibility) and records exactly which revision passed:

```sh
make check                         # in the container; CI runs the same on every push
python scripts/check.py --quick    # tests only, anywhere Python and the requirements are installed
```

## Setup

Everything runs inside the container, so no Python toolchain is needed on the
host. Start the practice server in one terminal:

```sh
docker compose up --build
```

Then, in a second terminal, generate the protobuf bindings once:

```sh
make proto
```

`bazaar_pb2.py` is generated, not committed, so this step is required on a fresh
checkout and after any schema change.

## Running

```sh
make run                                   # trade against the practice server
make run ARGS="--mode handshake"           # connect, confirm readiness, advertise once
make run ARGS="--mode walkthrough"         # replay the scripted ten-step exercise
```

Without a token the client reads `validation-credentials.json`, which the
practice server writes. Against a real server, pass credentials explicitly.

### Configuration

Nothing about the destination is compiled in. Every setting takes a flag or an
environment variable, flag winning:

| Flag | Environment | Default |
|---|---|---|
| `--ws-url` | `BAZAAR_WS_URL` | `ws://127.0.0.1:3001/ws` (`wss://` supported) |
| `--token` | `BAZAAR_TOKEN` | read from the credentials file |
| `--station-id` | `BAZAAR_STATION_ID` | `P01` |
| `--credentials-file` | `BAZAAR_CREDENTIALS_FILE` | `validation-credentials.json` |
| `--evidence-file` | `BAZAAR_EVIDENCE_FILE` | auto: `logs/live/<UTC timestamp>/<mode>-<station>-evidence.jsonl` for `trade`/`walkthrough`; none for `check`/`handshake` |
| `--no-evidence` | | off: disables the automatic evidence log for `trade`/`walkthrough` |
| `--log-level` | `BAZAAR_LOG_LEVEL` | `INFO` |
| `--mode` | `BAZAAR_MODE` | `trade` (also `check`, `handshake`, `walkthrough`) |
| `--strategy` | `BAZAAR_STRATEGY` | `reserve-trader` (also `passive`, a never-trading baseline) |
| `--status-every` | `BAZAAR_STATUS_EVERY` | `10`: log a status panel every N ticks, `0` off |
| `--status-file` | `BAZAAR_STATUS_FILE` | none: rewrite this file with the live panel (`.html` auto-refreshes) |
| `--max-decisions` | | none: stop after N decisions |
| `--version` | | print the build (branch@commit, clean or not) and exit |

Changing server or port needs no code change:

```sh
docker compose exec bazaar python -m bazaar_client.cli \
  --ws-url ws://127.0.0.1:3002/ws --credentials-file /tmp/other/creds.json
```

The token is wrapped so it cannot be printed by accident, and a logging filter
scrubs it from every record as a second line of defence. Credentials and reports
are gitignored.

`make` is not installed on Windows by default. Every Makefile target is a thin
wrapper, so run the command behind it directly, for example
`docker compose exec -T bazaar python -m bazaar_client.cli --mode trade ...`.

### Is it working? Status, and what a failure means

The client reports where it is on this ladder, in the log and in the evidence
file, so "running", "connected" and "playing" are never confused:

`starting` → `connecting` → `connected` (socket open) → `authenticated` (token
accepted, first state received) → `synchronized` (readiness confirmed) →
`waiting` (lobby or pause: idle by design) or `participating` (running and
deciding every tick) → `finished`. `stale` means the game is running but no new
state has arrived for three ticks, so our view is out of date; `disconnected`
means a reconnect is coming. `--mode check` walks the ladder without trading.

Every failure is reported with its category and what to check, and the exit
code tells scripts which it was:

| Exit | Category | Typical cause | What to check |
|---|---|---|---|
| 2 | configuration | no token, bad `--ws-url`, unknown `--strategy` | the flags and env vars above |
| 3 | authentication | HTTP 401/403, `INVALID_AUTHENTICATION` | the key; a restarted practice server issues new ones |
| 4 | protocol | HTTP 400, subprotocol not confirmed, bad message, wrong run | `make proto`; the server's version |
| 5 | network | refused, unresolvable host, timeout, dropped connection | is the server up? (retried automatically) |
| 1 | application | a bug in the client | rerun with `--log-level DEBUG`; the evidence log |

Only network failures are retried; the others would fail the same way again.

### Joining a live run

From PowerShell in the repo root. The key goes in an environment variable for
this terminal only, so it never lands in a file, the repo, or the command line
the container sees:

```powershell
docker compose up -d --build
docker compose exec -T bazaar scripts/gen_proto.sh
$env:BAZAAR_WS_URL = "wss://spaceport.edneo.com/ws"
$env:BAZAAR_TOKEN  = "<your key>"

# 1. Pre-flight: join, confirm readiness, report our planet, leave. Sends no trades.
docker compose exec -T -e BAZAAR_WS_URL -e BAZAAR_TOKEN bazaar python -m bazaar_client.cli --mode check

# 2. Play: leave this running for the whole run. Evidence logging is automatic
#    (see "Before a class run"); add --evidence-file to pick the path yourself.
docker compose exec -T -e BAZAAR_WS_URL -e BAZAAR_TOKEN bazaar python -m bazaar_client.cli --mode trade
```

`-e NAME` without a value forwards that variable from the terminal into the
container. The server works out which planet we are from the key, so no station
ID is needed. Start the client before the run begins: it waits on a quiet
connection instead of reconnecting, and reconnects by itself if the connection
really drops. `logs/` is gitignored.

### Before a class run

Deploy from a clean, committed checkout. The client logs its branch and commit
at startup (`client build main@65468f7bc8a9`), so a run log always says which
code played. In run 2 the offers on record were ones our committed code cannot
make, which means an older or modified build was deployed.

`trade` and `walkthrough` runs log evidence automatically now, so the run can
always be explained afterwards even if nobody remembers a flag: with no
`--evidence-file`, one is auto-timestamped at
`logs/live/<UTC timestamp>/<mode>-<station>-evidence.jsonl` (pass
`--evidence-file` yourself to pick the path, or `--no-evidence` to turn logging
off entirely). It is JSONL, one record per line, written and closed as it
happens so it survives a crash; a restart renames the previous file rather than
overwriting it. A run is framed by `run_start` (build, clean or not, strategy,
configuration) and `run_end` (counts, latency percentiles, seconds per status).
In between: `status` and `connection` changes, a `decision` record each tick
(what the strategy saw, including open offers and newly settled trades;
whether it acted or waited and why; why each incoming offer was passed over;
how long the state waited and the decision took) and one record per command
(its `decision_id`, `request_id`, result, the offer or transaction it created,
response and confirmation times, and whether it missed its tick). Those ids
connect every step of an offer. As soon as the run ends (or the client
crashes), a text summary and an HTML dashboard are written next to the
evidence log automatically — see "Run summary and dashboard" below.

For a live view during the run, add `--status-file logs/status.html` and open
it in a browser: it refreshes itself with reserves against targets, pending
actions, open offers and recent trades. The same panel is logged every
`--status-every` ticks.

### Analysing a Directorate run log

```sh
python scripts/analyze_run.py run-2-log.json --station P01
```

This prints every planet's outcome, how the galaxy's resources were used, and for
one station its offer terms against their outcomes plus a health and stock
timeline. It needs only the Python standard library, so other teams can run it
too.

### Run summary and dashboard from our own evidence log

For a real `trade`/`walkthrough` run, this is now done automatically the
moment the run ends (or crashes) — a `<mode>-<station>-summary.txt` and
`<mode>-<station>-dashboard.html` are written next to the evidence log, no
extra step needed. The command below remains useful to re-generate a
dashboard on demand, regenerate one for an older or rotated log, or trace a
specific `--offer`:

`scripts/analyze_evidence.py` turns the `--evidence-file` log from *our own*
client (decisions, commands sent, and connection events — see "Before a class
run" above) into a run summary, from the same terminal that ran the client or
any later one:

```sh
python scripts/analyze_evidence.py logs/live/20260101T000000Z/trade-P01-evidence.jsonl --html logs/report.html
```

Printed to the terminal: the build and strategy, **key findings** (the
dominant rejection reason, the worst shortage streak, the slowest pipeline
stage, the biggest single health drop, net trade balance, the widest gap with
no decisions logged — whichever apply), decisions logged, commands by kind,
rejections by code, completed trades, connection uptime/downtime and gaps in
the decision record, shortages, responsiveness (p50/p95/max per stage and
missed deadlines), acting versus waiting and why, stalls, time per status, and
a stock/health timeline.

`--html` additionally writes a self-contained, offline dashboard — open
`logs/report.html` directly in a browser, no server needed. A **Key findings**
panel at the top surfaces the same patterns as the text report, each one
clickable through to its tick; the KPI strip, chart, connection timeline and
commands/rejections panels below it are trimmed and collapse by default when
there's nothing notable, so the page stays scannable on a routine run:

- health and stock over time, hoverable, click a point to jump to that tick,
  click a legend entry to isolate one line
- a connection timeline showing when we were connected vs. not (collapsed
  unless there was downtime)
- commands-by-kind and rejections-by-code bar charts (collapsed unless
  something was rejected)
- responsiveness per stage, participation and the status history, completed
  trades with their net resource balance
- a searchable, filterable **stimuli &rarr; decision &rarr; outcome** table:
  what the policy saw, what it decided and why (or why it waited), which offers
  it passed over and why, and what the server answered, per tick

To reconstruct one offer end to end -- what we knew, what we decided and why,
what we sent, what the server confirmed, whether it became a trade:

```sh
python scripts/analyze_evidence.py logs/live/20260101T000000Z/trade-P01-evidence.jsonl --offer offer-37
```

Like `analyze_run.py`, it needs only the Python standard library and no
network access, so it also works outside the container and offline.

### Checking one decision without a server

```sh
python scripts/decide_once.py scenarios/unfair-offer.json
python scripts/decide_once.py scenarios/incoming-gift.json --strategy passive
```

A scenario file is a planet state plus the market, and an `expect` block; the
command prints the decision, its reasons and every pass, and exits non-zero if
the decision is not what the file expects. Every file in `scenarios/` is also a test.

## Our own server and local simulations

The practice server plays one fixed script. To play real, multi-tick games we
run our own server, which speaks the same protocol (details in
[SIMULATOR.md](SIMULATOR.md)):

```sh
# a server plus N separate client processes; logs, evidence and dashboards per planet
python -m bazaar_sim.orchestrate --planets 5 --ticks 60 --tick-ms 300
python -m bazaar_sim.orchestrate --planets 3 --strategies reserve-trader:2,passive:1

# just the server; point any client at it (ours, or another pair's)
python -m bazaar_sim.server --planets 3 --port 3100 --credentials-file sim-credentials.json
python -m bazaar_client.cli --ws-url ws://127.0.0.1:3100/ws --credentials-file sim-credentials.json --station-id P02
python -m bazaar_sim.server --planets 9 --open-auth    # ignore keys, seat planets in connection order
```

Production is balanced by default: for every resource the world makes exactly
what it consumes. The server writes a scored report when the run ends.

## Comparing strategies

```sh
python -m bazaar_sim.benchmark                                 # matched scenarios x seeds x seats
python -m bazaar_sim.benchmark --check logs/benchmark.json      # reproduce every row exactly
```

Success is defined before comparing (collective survival first; see
[BENCHMARKS.md](BENCHMARKS.md)), every candidate plays identical cases, and
failed runs are listed, not hidden.

## Tests

```sh
make check             # everything, as CI runs it; writes logs/check-report.json
make test              # unit, wire, state handling, survival, simulator and server
make test-integration  # against a real practice server it starts itself
make cov               # full suite, including integration, with branch coverage
```

691 tests (686 without the practice server); 97% combined statement/branch
coverage of handwritten code with the integration tests (`make cov`). The integration tests start their
own `bazaar-server` on a free port, so they are repeatable and do not disturb
the instance from `docker compose up`. `.github/workflows/ci.yml` runs
`scripts/check.py --integration` on every push and pull request and keeps the
check report, which names the exact commit tested.

Worth knowing: the practice server runs **one fixed script**. It proves the wire
format, handshake and command set exactly, and it cannot exercise the trading
policy — any unscripted command ends the exercise as `scenario mismatch`, by
design. The policy is covered by unit tests and simulated multi-tick
economies, including nine planets with variable production, delayed acceptance,
temporary outages, a replay of the Directorate's run 2, and full runs against
stand-ins for the clients seen in that run, measuring how long the world and our
planet survive (`tests/survival/test_world_survival.py`). The trading rules
(one-for-one, paid only in our specialty) are checked on every action across a
grid of situations. See [ARCHITECTURE.md](ARCHITECTURE.md#testing).

## Layout

```
bazaar_client/
  domain/      types and protobuf translation
  codec/       bytes on the wire
  connection/  socket, handshake, request ids, throttle
  world/       snapshot view, commitments, counterparty inference
  policy/      the trading decision
  execution/   actions, sending, evidence log
  autonomous.py         the trading loop
  strategy.py           strategies selectable by name (--strategy)
  status.py             the connection/participation status ladder, staleness
  status_view.py        the human-readable status panel
  metrics.py            latency percentiles and missed deadlines
  diagnostics.py        failure categories, hints and exit codes
  scripted_walkthrough.py  the practice exercise replay
  version.py            which build is running, clean or not
bazaar_sim/
  economy.py            the rule engine: production, upkeep, health, settlement
  server.py             our Bazaar server over the real protocol
  codec.py              the server's protobuf boundary
  orchestrate.py        server + N client processes, one command
  opponents.py          stand-ins for the clients seen in run 2
  world.py              whole-world runs, balanced production, the success score
  benchmark.py          matched comparisons, reproducibility check
scenarios/              example situations with the decision each expects
scripts/
  gen_proto.sh          protobuf codegen
  check.py              every check in one command
  analyze_run.py        summarise a Directorate run log
  analyze_evidence.py   summarise our own evidence log; HTML dashboard; one offer's story
  decide_once.py        one decision from a scenario file, no server
```

After dependency or Dockerfile changes, rebuild the running service with
`docker compose up -d --build` before using the Makefile targets. To test in a
fresh container without restarting an existing practice exercise:

```sh
docker compose build
docker compose run --rm --no-deps bazaar sh -c 'scripts/gen_proto.sh && pytest --cov=bazaar_client --cov-branch'
```
