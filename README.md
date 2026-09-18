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
make cov               # coverage report
```

385 tests, 91% coverage of handwritten code. The integration tests start their
own `bazaar-server` on a free port, so they are repeatable and do not disturb
the instance from `docker compose up`.

Worth knowing: the practice server runs **one fixed script**. It proves the wire
format, handshake and command set exactly, and it cannot exercise the trading
policy — any unscripted command ends the exercise as `scenario mismatch`, by
design. The policy is covered by unit tests and by a simulated multi-tick
economy instead. See [ARCHITECTURE.md](ARCHITECTURE.md#testing).

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
