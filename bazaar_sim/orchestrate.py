"""Run our server and several independent client processes, then report.

    python -m bazaar_sim.orchestrate --planets 3 --ticks 30 --tick-ms 300
    python -m bazaar_sim.orchestrate --planets 5 --strategies reserve-trader:4,passive:1

Each client is a separate `python -m bazaar_client.cli --mode trade` process
talking to the server over a real socket, exactly as in a class run. Output
goes to one folder: the server's scored report and log, and per planet its
client log, JSONL evidence and Markdown summary. --live opens a browser view.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from bazaar_client.strategy import STRATEGIES

REPO_ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def assign_strategies(spec: str, planets: int) -> list[str]:
    """"reserve-trader" for everyone, or "reserve-trader:4,passive:1" summing to planets."""
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if len(parts) == 1 and ":" not in parts[0]:
        parts = [f"{parts[0]}:{planets}"]
    assigned: list[str] = []
    for part in parts:
        name, _, count = part.partition(":")
        if name not in STRATEGIES:
            raise ValueError(f"unknown strategy {name!r}; choose from {', '.join(sorted(STRATEGIES))}")
        assigned += [name] * int(count or 1)
    if len(assigned) != planets:
        raise ValueError(f"strategies cover {len(assigned)} planets, but there are {planets}")
    return assigned


def client_environment() -> dict[str, str]:
    """No inherited BAZAAR_* settings: each client must use exactly what it is given."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BAZAAR_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    return env


async def _wait_for_file(path: Path, process: asyncio.subprocess.Process, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if process.returncode is not None:
            raise RuntimeError(f"server exited early with code {process.returncode}")
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"{path} did not appear within {timeout}s")
        await asyncio.sleep(0.1)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def _summaries(out: Path, stations: list[str]) -> dict[str, str]:
    from bazaar_client.execution.reporting import load_analyzer

    analyzer = load_analyzer()
    written = {}
    for sid in stations:
        result = analyzer.write_reports(out / f"{sid}-evidence.jsonl", title=f"{sid} run report")
        if result:
            written[sid] = str(result["summary"])
    return written


async def orchestrate(args: argparse.Namespace) -> dict:
    mode = getattr(args, "mode", "trade")
    if mode == "hivemind":
        import importlib.util
        try:
            installed = importlib.util.find_spec("spaceport_hivemind.client") is not None
        except ModuleNotFoundError:
            installed = False
        if not installed:
            raise ValueError("install Hivemind first: python -m pip install -r requirements-hivemind.txt")
    strategies = ["hivemind"] * args.planets if mode == "hivemind" else assign_strategies(args.strategies, args.planets)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or REPO_ROOT / "logs" / "sim" / stamp
    out.mkdir(parents=True, exist_ok=True)
    if (out / "credentials.json").exists():
        raise ValueError(f"{out} already contains a run; choose a new --out directory")
    port = args.port or free_port()
    url = f"ws://127.0.0.1:{port}/ws"
    credentials = out / "credentials.json"
    report_path = out / "server-report.json"
    env = client_environment()

    server_cmd = [
        sys.executable, "-m", "bazaar_sim.server", "--port", str(port),
        "--planets", str(args.planets), "--ticks", str(args.ticks), "--tick-ms", str(args.tick_ms),
        "--production", args.production, "--variation", str(args.variation),
        "--surplus", str(args.surplus),
        "--starting-stock", str(args.starting_stock),
        "--credentials-file", str(credentials), "--report", str(report_path),
    ]
    with open(out / "server.log", "w", encoding="utf-8") as server_log:
        server = await asyncio.create_subprocess_exec(
            *server_cmd, stdout=server_log, stderr=asyncio.subprocess.STDOUT, env=env, cwd=REPO_ROOT)
    clients: dict[str, asyncio.subprocess.Process] = {}
    coordinator = None
    hive_url = None
    try:
        if mode == "hivemind":
            hive_port = free_port()
            hive_url = f"ws://127.0.0.1:{hive_port}"
            with open(out / "hivemind-server.log", "w", encoding="utf-8") as hive_log:
                coordinator = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", "from spaceport_hivemind.server import main; main()",
                    "--host", "127.0.0.1", "--port", str(hive_port),
                    "--station-count", str(args.planets),
                    stdout=hive_log, stderr=asyncio.subprocess.STDOUT, env=env, cwd=REPO_ROOT)
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                if coordinator.returncode is not None:
                    raise RuntimeError("Hivemind server failed; see hivemind-server.log")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", hive_port)
                    writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{hive_port}\r\nConnection: close\r\n\r\n".encode())
                    await writer.drain()
                    await asyncio.wait_for(reader.read(), 2)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    if asyncio.get_running_loop().time() > deadline:
                        raise TimeoutError("Hivemind server did not start")
                    await asyncio.sleep(.05)
            print(f"Hivemind coordinator on {hive_url}", flush=True)
        await _wait_for_file(credentials, server, timeout=20)
        print(f"server on {url}; output in {out}", flush=True)
        for i, strategy in enumerate(strategies):
            sid = f"P{i + 1:02}"
            cmd = [
                sys.executable, "-m", "bazaar_client.cli", "--mode", mode, "--ws-url", url,
                "--credentials-file", str(credentials), "--station-id", sid,
                "--evidence-file", str(out / f"{sid}-evidence.jsonl"),
                "--status-every", "0", "--log-level", args.client_log_level,
            ]
            if mode == "hivemind":
                cmd += ["--hivemind-endpoint", hive_url, "--hivemind-exit-on-finish"]
            else:
                cmd += ["--strategy", strategy]
            with open(out / f"{sid}-client.log", "w", encoding="utf-8") as log:
                clients[sid] = await asyncio.create_subprocess_exec(
                    *cmd, stdout=log, stderr=asyncio.subprocess.STDOUT, env=env, cwd=REPO_ROOT)
            print(f"  {sid} running {strategy}", flush=True)

        run_seconds = args.ticks * args.tick_ms / 1000
        await asyncio.wait_for(server.wait(), timeout=run_seconds + args.grace)
        exit_codes = {}
        for sid, process in clients.items():
            try:
                exit_codes[sid] = await asyncio.wait_for(process.wait(), timeout=args.grace)
            except asyncio.TimeoutError:
                await _terminate(process)
                exit_codes[sid] = "timed out"
    finally:
        for process in clients.values():
            await _terminate(process)
        await _terminate(server)
        if coordinator is not None:
            await _terminate(coordinator)

    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
    summary = {
        "out": str(out),
        "url": url,
        "mode": mode,
        "hivemind_url": hive_url,
        "strategies": dict(zip((f"P{i + 1:02}" for i in range(args.planets)), strategies)),
        "client_exit_codes": exit_codes,
        "server_report": report,
        "summaries": _summaries(out, list(clients)),
    }
    (out / "orchestration.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def print_summary(summary: dict) -> None:
    report = summary["server_report"]
    if report is None:
        print("the server did not write a report; see server.log")
        return
    score = report["score"]
    print(f"\nrun {report['run_id']}: {score['survivors']} of {score['planets']} planets survived "
          f"{report['tick']} ticks; collective success {score['collective_success']}; "
          f"{report['transactions']} trades")
    print(f"{'planet':7} {'strategy':15} {'client exit':12} {'health':>6} {'failed at':>9} "
          f"{'inventory w/f/c':>16} {'imported':>12}")
    for planet in report["planets"]:
        sid = planet["station_id"]
        inv = "/".join(str(v) for v in planet["inventory"].values())
        imp = "/".join(str(v) for v in planet["imported"].values())
        print(f"{sid:7} {summary['strategies'][sid]:15} {str(summary['client_exit_codes'].get(sid)):12} "
              f"{planet['health']:>6} {str(planet['first_failure_tick'] or '-'):>9} {inv:>16} {imp:>12}")
    for sid, page in summary["summaries"].items():
        print(f"summary {sid}: {page}")
    print(f"everything is in {summary['out']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bazaar-orchestrate", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("trade", "hivemind"), default="trade",
                        help="local policy or a shared Hivemind coordinator")
    parser.add_argument("--planets", type=int, default=3)
    parser.add_argument("--strategies", default="reserve-trader",
                        help='one name for every planet, or "reserve-trader:2,passive:1"')
    parser.add_argument("--ticks", type=int, default=30)
    parser.add_argument("--tick-ms", type=int, default=300)
    parser.add_argument("--production", choices=("balanced", "run2"), default="balanced")
    parser.add_argument("--variation", type=int, default=0)
    parser.add_argument("--surplus", type=float, default=0.0)
    parser.add_argument("--starting-stock", type=int, default=30)
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--grace", type=float, default=60.0,
                        help="seconds allowed beyond the run's length before giving up")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--client-log-level", default="INFO")
    parser.add_argument("--no-dashboards", dest="dashboards", action="store_false", help=argparse.SUPPRESS)
    parser.add_argument("--live", action="store_true", help="open a live browser dashboard; keep it available until Ctrl+C")
    parser.add_argument("--dashboard-port", type=int, default=8766)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dashboard = None
    if args.live:
        args.out = args.out or REPO_ROOT / "logs" / "sim" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        if (args.out / "credentials.json").exists():
            print("Choose a new --out directory for this simulation.", file=sys.stderr)
            return 2
        args.out.mkdir(parents=True, exist_ok=True)
        ready = args.out / "dashboard-url.txt"
        ready.unlink(missing_ok=True)
        dashboard = subprocess.Popen([
            sys.executable, "-m", "bazaar_client.live_dashboard", str(args.out),
            "--port", str(args.dashboard_port), "--open", "--ready-file", str(ready),
        ], cwd=REPO_ROOT)
    try:
        if dashboard:
            deadline = time.monotonic() + 10
            while not ready.exists():
                if dashboard.poll() is not None:
                    raise ValueError("dashboard could not start; try a different --dashboard-port")
                if time.monotonic() > deadline:
                    raise ValueError("dashboard startup timed out")
                time.sleep(.02)
        summary = asyncio.run(orchestrate(args))
        print_summary(summary)
        if dashboard:
            print("Simulation complete. The live page remains available; press Ctrl+C to close it.", flush=True)
            dashboard.wait()
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    finally:
        if dashboard and dashboard.poll() is None:
            dashboard.terminate()
            try:
                dashboard.wait(timeout=3)
            except subprocess.TimeoutExpired:
                dashboard.kill()
                dashboard.wait()
    report = summary["server_report"]
    clean = report is not None and report["phase"] == "FINISHED" and all(
        code == 0 for code in summary["client_exit_codes"].values())
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
