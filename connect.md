# Connecting to the live server

A short runbook for joining the live Spaceport Bazaar server as P01 and watching
the run. For everything else see [README.md](README.md).

## All nine planets at once (Wednesday demo)

The nine keys and the endpoint live in `my-credentials.json` at the repo root
(git-ignored; never commit it). From the repo root in PowerShell:

```powershell
.\fleet.ps1 check   # ~6 s: each key joins, confirms readiness, leaves. No trades.
.\fleet.ps1         # all nine trade until the run ends; opens a 9-panel status grid
.\fleet.ps1 stop    # kill any client left in the container
```

- Run `check` a few minutes before the demo. Docker is then already up, so
  `.\fleet.ps1` has all nine planets connected within a few seconds.
- The terminal prints every planet's tick, health and stock every 10 s.
  Everything goes to `logs/live/fleet-<time>/`: `fleet.html`, and for each
  planet `Pxx-status.html`, `Pxx-evidence.jsonl`, `Pxx-client.log`, and
  `Pxx-summary.md` once the run ends.
- Ctrl+C stops all nine (they drop out of the run). `.\fleet.ps1` always clears
  leftover clients first, so two clients never share a key.
- Another endpoint: edit `ws_url` in `my-credentials.json`, or run
  `docker compose exec -T bazaar python scripts/fleet.py trade --ws-url <url>`.

- Endpoint: `wss://spaceport.edneo.com/ws`
- Token: given to us per run. **Never commit it or paste it into a file in the repo.**

## 1. Start Docker

Open Docker Desktop, wait until it says it is running, then from the repo root:

```sh
docker compose up -d --build
docker compose exec bazaar scripts/gen_proto.sh     # once per fresh checkout / schema change
```

On Git Bash for Windows, prefix `docker compose exec` commands with
`MSYS_NO_PATHCONV=1` so paths are not rewritten.

## 2. Put the token in the environment

Keeps it out of your shell history and the command lines below:

```sh
export BAZAAR_TOKEN=<token>          # Git Bash
$env:BAZAAR_TOKEN = "<token>"        # PowerShell
```

Pass it into the container with `-e BAZAAR_TOKEN` on the `docker compose exec`
commands (or use `--token <token>` instead).

## 3. Check the connection first (safe)

Joins, confirms readiness, prints our planet and leaves. Sends no offers or
trades:

```sh
docker compose exec -e BAZAAR_TOKEN bazaar python -m bazaar_client.cli \
  --mode check --ws-url wss://spaceport.edneo.com/ws
```

Success ends with `readiness confirmed: this key and endpoint are ready to trade`.

## 4. Join and trade

```sh
mkdir -p logs/live/session
docker compose exec -e BAZAAR_TOKEN -e PYTHONUNBUFFERED=1 bazaar python -m bazaar_client.cli \
  --mode trade --ws-url wss://spaceport.edneo.com/ws \
  --evidence-file logs/live/session/trade-P01-evidence.jsonl \
  --status-file logs/live/session/status.html
```

Run it in its own terminal and leave it running. The run is 120 ticks. Stopping
the client mid-run drops us from the simulation.

## 5. Open the dashboards

Two views, both from files in `logs/live/session/`:

| File | What it shows |
|---|---|
| `status.html` | Live status panel: tick, health, reserves, pending actions, open offers, recent trades. Written by the client and auto-refreshes. |
| `live-dashboard.html` | The full **trades and supplies** dashboard: resource and health history, completed trades, rejected commands, shortages, responsiveness. |

`live-dashboard.html` is built from the evidence log by
`scripts/analyze_evidence.py`, which is a one-off snapshot. To keep it live, in a
third terminal regenerate it every few seconds (the loop also makes the page
reload itself):

```sh
cd logs/live/session
while true; do
  python ../../../scripts/analyze_evidence.py trade-P01-evidence.jsonl --html live-dashboard.html >/dev/null
  sed -i '0,/<head>/s//<head><meta http-equiv="refresh" content="5">/' live-dashboard.html
  sleep 5
done
```

(The analyzer rewrites the file from scratch on every pass, so the `sed` step
never doubles the tag.) Then open `live-dashboard.html` in a browser.
When the client exits it also writes `trade-P01-summary.txt` and
`trade-P01-dashboard.html` next to the evidence file: the final report.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `HTTP 502` or `HTTP 530` on connect | The server (or its tunnel) is down. Nothing to fix on our side. Retry every minute or so. |
| Stuck at `tick 0 ... READY` | The server has not started the run, often because it is waiting for the other planets. Stay connected. |
| `token rejected` / auth failure | Wrong or expired token, or the wrong station. Check `--station-id` (default `P01`). |
| `argument --mode: invalid choice: 'check'`, or `no attribute 'strategy'` | The config/CLI got out of sync in a merge. Check that `config.py` still defines `--strategy`, `--status-every`, `--status-file` and `check` mode. |
| `Decision has no attribute 'targets'` | The status view or `strategy.py` is reading fields from the old policy. The current `Decision` has `reserve`, `available`, `surplus`, `deficit`, `urgency`. |
| `bazaar_pb2` import error | Run the `gen_proto.sh` step above. |

## Only one client per station

Never run two clients on the same station and token. If a run looks wrong, stop
the client before starting another (`docker compose restart bazaar` kills any
leftover process).
