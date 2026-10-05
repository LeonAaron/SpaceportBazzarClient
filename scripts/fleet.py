"""Connect all nine planets at once, each with its own key.

    python scripts/fleet.py check     # join, confirm every key, leave (no trades)
    python scripts/fleet.py trade     # trade with all nine until the run ends

Keys come from my-credentials.json (git-ignored), never the command line:

    {"ws_url": "wss://...", "players": [{"station_id": "P01", "token": "..."}, ...]}

Every client starts in parallel, so the whole fleet is connected in a few
seconds. In trade mode each planet writes its evidence and status page under
logs/live/fleet-<time>/, and fleet.html shows all nine status pages on one
screen. Ctrl+C stops every client.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

JOINED = re.compile(r"joined run (\S+) as (\S+).*?tick (\d+) of (\d+)")
READY = "readiness confirmed: this key and endpoint are ready to trade"
FINAL = re.compile(r"final: health=(\d+) inventory=(\{.*?\})")
PROGRESS = re.compile(r"\| tick (\d+) health (\d+) inventory (\{.*?\})")
WAITING = re.compile(r"\| (P\d+) \| tick (\d+)/(\d+) \| (\w+)")
FAILURE = re.compile(r"token rejected|auth\w* fail|HTTP [45]\d\d|Traceback|ERROR")


def load_credentials(path: Path) -> tuple[str | None, list[dict]]:
    document = json.loads(path.read_text())
    players = document.get("players", [])
    if not players:
        sys.exit(f"{path} lists no players")
    return document.get("ws_url"), players


def launch(mode: str, ws_url: str, credentials: Path, players: list[dict], out: Path):
    out.mkdir(parents=True, exist_ok=True)
    procs = {}
    for player in players:
        sid = player["station_id"]
        cmd = [
            sys.executable, "-m", "bazaar_client.cli", "--mode", mode,
            "--ws-url", ws_url, "--credentials-file", str(credentials), "--station-id", sid,
        ]
        if mode == "trade":
            cmd += ["--evidence-file", str(out / f"{sid}-evidence.jsonl"),
                    "--status-file", str(out / f"{sid}-status.html")]
        else:
            cmd += ["--no-evidence"]
        log = open(out / f"{sid}-client.log", "w")
        procs[sid] = (subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT,
            env={**_env(), "PYTHONUNBUFFERED": "1"},
        ), log)
    return procs


def _env():
    import os
    env = dict(os.environ)
    env.pop("BAZAAR_TOKEN", None)  # each planet must use its own key from the file
    return env


def read_log(out: Path, sid: str) -> str:
    try:
        return (out / f"{sid}-client.log").read_text(errors="replace")
    except OSError:
        return ""


def describe(text: str) -> str:
    final = FINAL.findall(text)
    if final:
        health, inventory = final[-1]
        return f"finished  health {health}  {inventory}"
    problem = [line for line in text.splitlines() if FAILURE.search(line)]
    progress = PROGRESS.findall(text)
    waiting = WAITING.findall(text)
    if progress:
        tick, health, inventory = progress[-1]
        line = f"tick {tick}  health {health}  {inventory}"
    elif waiting:
        _, tick, total, phase = waiting[-1]
        line = f"{phase} at tick {tick}/{total}, waiting for the run to start"
    elif JOINED.findall(text):
        run, _, tick, total = JOINED.findall(text)[-1]
        line = f"{run} tick {tick}/{total}"
    else:
        line = "connecting..."
    return f"{line}  !! {problem[-1][-90:]}" if problem else line


def write_grid(out: Path, players: list[dict]) -> Path:
    frames = "\n".join(
        f'<div><h2>{p["station_id"]}</h2>'
        f'<iframe src="{p["station_id"]}-status.html"></iframe></div>'
        for p in players
    )
    page = out / "fleet.html"
    page.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>Fleet status</title>
<style>
body{{margin:0;padding:12px;font:14px system-ui,sans-serif;background:#f6f6f4;color:#222}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:10px}}
h2{{margin:4px 0;font-size:15px}} iframe{{width:100%;height:340px;border:1px solid #ccc;background:#fff}}
@media (prefers-color-scheme:dark){{body{{background:#1b1b1b;color:#eee}}iframe{{border-color:#444}}}}
</style></head><body><main>
{frames}
</main></body></html>""")
    return page


def run_check(procs, out, players, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(p.poll() is None for p, _ in procs.values()):
        time.sleep(0.5)
    ok = 0
    print(f"\n{'planet':7} {'result':8} detail")
    for player in players:
        sid = player["station_id"]
        proc, log = procs[sid]
        if proc.poll() is None:
            proc.terminate()
        log.close()
        text = read_log(out, sid)
        joined = JOINED.findall(text)
        passed = READY in text and joined and joined[-1][1] == sid
        ok += bool(passed)
        detail = (f"run {joined[-1][0]}, tick {joined[-1][2]} of {joined[-1][3]}"
                  if passed else describe(text))
        print(f"{sid:7} {'READY' if passed else 'FAILED':8} {detail}")
    print(f"\n{ok} of {len(players)} keys ready")
    return 0 if ok == len(players) else 1


def run_trade(procs, out, players, refresh):
    print(f"all {len(players)} clients started; status grid: {out / 'fleet.html'}")
    print("Ctrl+C stops every client (and drops the planets from the run).\n")
    try:
        while any(p.poll() is None for p, _ in procs.values()):
            time.sleep(refresh)
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"--- {stamp}")
            for player in players:
                sid = player["station_id"]
                proc, _ = procs[sid]
                state = "running" if proc.poll() is None else f"exited {proc.returncode}"
                print(f"{sid}  {state:10} {describe(read_log(out, sid))}")
    except KeyboardInterrupt:
        print("\nstopping every client...")
        for proc, _ in procs.values():
            if proc.poll() is None:
                proc.terminate()
    for proc, log in procs.values():
        proc.wait()
        log.close()
    print("\nfinal:")
    for player in players:
        sid = player["station_id"]
        print(f"{sid}  {describe(read_log(out, sid))}")
    print(f"\neverything is in {out}")
    return 0 if all(p.returncode == 0 for p, _ in procs.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check", "trade"))
    parser.add_argument("--credentials", type=Path, default=Path("my-credentials.json"))
    parser.add_argument("--ws-url", help="overrides ws_url from the credentials file")
    parser.add_argument("--out", type=Path, help="default logs/live/fleet-<time>")
    parser.add_argument("--timeout", type=float, default=30.0, help="check mode: seconds to wait")
    parser.add_argument("--refresh", type=float, default=10.0, help="trade mode: seconds between progress lines")
    args = parser.parse_args(argv)

    file_url, players = load_credentials(args.credentials)
    ws_url = args.ws_url or file_url
    if not ws_url:
        sys.exit("no endpoint: add ws_url to the credentials file or pass --ws-url")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or Path("logs/live") / f"fleet-{args.mode}-{stamp}"

    print(f"{args.mode}: {len(players)} planets -> {ws_url}")
    procs = launch(args.mode, ws_url, args.credentials, players, out)
    if args.mode == "check":
        return run_check(procs, out, players, args.timeout)
    write_grid(out, players)
    return run_trade(procs, out, players, args.refresh)


if __name__ == "__main__":
    sys.exit(main())
