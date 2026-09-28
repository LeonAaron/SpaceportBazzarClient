# Spaceport Bazaar client

A Python client for the Spaceport Bazaar trading simulation. It connects over
WebSocket, speaks binary Protobuf, keeps its planet supplied, and trades with
the other planets.

- [ARCHITECTURE.md](ARCHITECTURE.md) — design, the trading policy, and what the
  tests do and do not prove
- [DOCKER.md](DOCKER.md) — the container workflow
- [SPECIFICATIONS.md](SPECIFICATIONS.md) — the assignment

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
| `--evidence-file` | `BAZAAR_EVIDENCE_FILE` | none |
| `--log-level` | `BAZAAR_LOG_LEVEL` | `INFO` |
| `--mode` | `BAZAAR_MODE` | `trade` |

Changing server or port needs no code change:

```sh
docker compose exec bazaar python -m bazaar_client.cli \
  --ws-url ws://127.0.0.1:3002/ws --credentials-file /tmp/other/creds.json
```

The token is wrapped so it cannot be printed by accident, and a logging filter
scrubs it from every record as a second line of defence. Credentials and reports
are gitignored.

## Tests

```sh
make test              # unit, wire, state handling, survival
make test-integration  # against a real practice server it starts itself
make cov               # full suite, including integration, with branch coverage
```

420 tests; 94% combined statement/branch coverage of handwritten code. The integration tests start their
own `bazaar-server` on a free port, so they are repeatable and do not disturb
the instance from `docker compose up`.

Worth knowing: the practice server runs **one fixed script**. It proves the wire
format, handshake and command set exactly, and it cannot exercise the trading
policy — any unscripted command ends the exercise as `scenario mismatch`, by
design. The policy is covered by unit tests and simulated multi-tick
economies, including nine planets with variable production, delayed acceptance,
and temporary outages. See [ARCHITECTURE.md](ARCHITECTURE.md#testing).

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
  scripted_walkthrough.py  the practice exercise replay
```

After dependency or Dockerfile changes, rebuild the running service with
`docker compose up -d --build` before using the Makefile targets. To test in a
fresh container without restarting an existing practice exercise:

```sh
docker compose build
docker compose run --rm --no-deps bazaar sh -c 'scripts/gen_proto.sh && pytest --cov=bazaar_client --cov-branch'
```

Trading continuously offers surplus production for the two imported resources,
even while stocks are healthy. Import prices use uncommitted stock:

| Import stock | Offered payment per unit received |
| --- | --- |
| 20+ | 1 |
| 15–19 | 1.2 |
| 10–14 | 1.5 |
| 9 / 8 / 7 | 2 / 2.25 / 2.5 |
| Below 7 | 2.5 × 1.25^(7 − stock), capped at 8 |

Trades normally request four units; the 1.2 tier requests five so six units of
payment express the price exactly. Other prices round to whole units. Incoming
production-for-import offers use the same curve and are evaluated before
advertising. Advertisements continuously list production for sale and imports
as wanted, renewing before expiry.

Production protection uses the balance **after payment is reserved**, including
all standing offers. Normal offers retain 25 ticks of specialty upkeep; trades
at 0.5:1 or better retain 10 ticks. Below 10 ticks no paid trades are permitted.
New offers and acceptances cannot invalidate standing normal offers by dropping
the balance below 25 ticks. Outgoing gifts also retain 25 ticks; free incoming
gifts remain welcome. These buffers allow normal trades from the 30-unit start.

When **every** resource is above 30 uncommitted units, stop advertising and
withdraw any active advertisement. Continue seeking trades, but accept only
offers with a strict net gain in total units and no received units of our own
produced resource (including mixed bundles). At 30 or below in any resource,
normal advertising and acceptance resume.

Excess above 32 uncommitted units of any resource builds a protected storage
balance within inventory. The upkeep reserve plus this stored balance is capped
at 15 units per resource. Already stored units are excluded when calculating
new excess, so repeated snapshots do not count the same stock twice. Storage
persists when inventory falls and can be consumed by upkeep, but not trades.
There is no separate server-side storage or deposit command.

## Hivemind mode

The [upstream Hivemind README](spaceport_hivemind/README.md) describes a central
coordinator: bridges forward Bazaar state and execute its explicit commands.
This checkout pins that repository as a Git submodule. Initialize it on a fresh
checkout, then install its optional package into the same environment as this
client:

```sh
git submodule update --init --recursive
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -e ./spaceport_hivemind
```

In separate terminals, start the demo game and coordinator:

```sh
.venv/bin/hivemind-demo-server --credential-file /tmp/hive-demo-credentials.json \
  --stations P01,P02,P03 --duration-ticks 30 --tick-duration-ms 1000

.venv/bin/hivemind-server --host 127.0.0.1 --port 8765 \
  --shared-key local-hive-key --station-count 3
```

Start one assignment client for each station, changing `P01` to `P02` and `P03`:

```sh
BAZAAR_HIVEMIND_KEY=local-hive-key .venv/bin/python -m bazaar_client.cli \
  --mode hivemind --station-id P01 \
  --credentials-file /tmp/hive-demo-credentials.json \
  --ws-url ws://127.0.0.1:3001/ws \
  --hivemind-endpoint ws://127.0.0.1:8765
```

`--hivemind-endpoint` also accepts `BAZAAR_HIVEMIND_ENDPOINT`;
`--hivemind-key` overrides `BAZAAR_HIVEMIND_KEY`. Existing game-token flags,
environment variables, and credentials files work in this mode. The game token
stays at the bridge; the coordinator receives its separate shared key. Both
secrets are redacted by this client's logging setup.

Hivemind mode delegates to upstream `HivemindClient`, using its binary Bazaar
connection and JSON coordinator protocol. The local trading policy, autonomous
reconnect supervisor, evidence log, and `--max-decisions` do not run in this
mode. A connection failure exits through the CLI error handler. The default
`--mode trade` still runs the assignment policy. Run only one client per station.
For Docker, install the optional package inside the running container with
`docker compose exec bazaar pip install -e /workspace/spaceport_hivemind` and use
endpoints reachable from that container.

All configured bridges must connect before the demo game starts. The assignment
practice server accepts only P01 and follows a fixed script, so use the hivemind
demo server for coordinated trading. Verify this client's adapter with:

```sh
.venv/bin/python -m pytest tests/unit/test_hivemind.py tests/integration/test_hivemind_demo.py -q
```

### Rebase validation

The assignment policy was preserved when rebasing onto the incoming optimization
branch. Incoming decision logging, build identification, run analysis, and
counterparty observations remain; decision logs now report the assignment's
reserve and surplus. The grid tests enforce the assignment price cap instead
of the replaced strategy's fixed 1:1 pricing.

The suite is not fully green: seven five-unit-start simulation failures also
reproduce on the original assignment commit (`26b9909`), whose production guards
prevent recovery in those scenarios. Five incoming world-simulation assertions
also fail with the preserved policy: four expect stronger survival against
quitting suppliers and one requires every trade to be 1:1. These assertions remain
visible. The earlier coverage and test-count figures above are historical, not
validation of this combined version. The original commit is saved locally as
`backup/assignment-before-hivemind`.
