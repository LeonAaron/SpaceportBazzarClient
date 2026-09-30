"""Read-only live view of append-only evidence logs, in a separate process.

No trading code waits for this service. Disk tails are incremental; each browser
has one replaceable pending frame. Slow viewers skip intermediate frames while
the evidence files retain every event.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import webbrowser
from pathlib import Path

from websockets.asyncio.server import serve

PAGE = Path(__file__).with_name("live_dashboard.html")


class Feed:
    def __init__(self, directory: Path):
        self.directory = directory
        self.files = {}
        self.stations = {}
        self.revision = 0
        self.next_scan = 0.0
        self.report_stamp = None
        self.finished = False
        self.final_report = None

    def ingest(self, sid, record):
        kind = record.get("kind")
        if kind == "run_start":
            self.stations.pop(sid, None)
            self.finished = False
        station = self.stations.setdefault(sid, {
            "id": sid, "status": "connecting", "tick": None, "health": None,
            "inventory": {}, "reserve": {}, "actions": [], "reasons": [],
            "events": [], "trades": [], "history": [], "confirmed": 0, "rejected": 0,
        })
        station["updated_ms"] = time.time() * 1000
        if kind == "run_start":
            station["strategy"] = record.get("strategy", "unknown")
        elif kind == "decision":
            for key in ("tick", "health", "inventory", "reserve", "available", "actions", "reasons", "phase"):
                if key in record:
                    station[key] = record[key]
            station["offers"] = record.get("open_offers", [])[-20:]
            point = {"tick": record["tick"], "health": record["health"], **record["inventory"]}
            if station["history"] and station["history"][-1]["tick"] == point["tick"]:
                station["history"][-1] = point
            else:
                station["history"] = (station["history"] + [point])[-120:]
            for trade in record.get("new_transactions", []):
                station["trades"] = ([trade] + station["trades"])[:12]
        elif kind == "status":
            station["status"] = record.get("status", "unknown")
        elif kind == "connection":
            station["connection"] = record.get("event")
        elif kind == "run_end":
            station["ended"] = True
        elif "action_kind" in record:
            if record.get("result_ok") is True:
                station["confirmed"] += 1
            elif record.get("result_ok") is False:
                station["rejected"] += 1
            station["events"] = ([{
                "kind": "command", "action": record["action_kind"],
                "ok": record.get("result_ok"), "code": record.get("result_code", "No confirmation"),
                "tick": record.get("processed_tick", record.get("observed_tick")),
            }] + station["events"])[:12]
        self.revision += 1

    def poll(self):
        previous_revision = self.revision
        now = time.monotonic()
        if now >= self.next_scan:
            # Rotated evidence logs contain a timestamp after 'evidence', so do
            # not match this suffix. Never read credentials or arbitrary files.
            for path in self.directory.glob("*-evidence.jsonl"):
                self.files.setdefault(path, {"offset": 0, "partial": b"", "inode": None})
            self.next_scan = now + .25
        for path, cursor in self.files.items():
            try:
                stat = path.stat()
                sid = path.name.removesuffix("-evidence.jsonl")
                if cursor["inode"] != stat.st_ino or stat.st_size < cursor["offset"]:
                    cursor.update(offset=0, partial=b"", inode=stat.st_ino)
                    self.stations.pop(sid, None)
                if stat.st_size == cursor["offset"]:
                    continue
                with path.open("rb") as handle:
                    handle.seek(cursor["offset"])
                    chunk = handle.read(262144)
                    cursor["offset"] = handle.tell()
                lines = (cursor["partial"] + chunk).split(b"\n")
                cursor["partial"] = lines.pop()
                if len(cursor["partial"]) > 2**20:
                    cursor["partial"] = b""
                for line in lines:
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            self.ingest(sid, record)
                    except (ValueError, KeyError, TypeError):
                        continue
            except FileNotFoundError:
                continue
        report = self.directory / "server-report.json"
        try:
            stamp = report.stat().st_mtime_ns
            if stamp != self.report_stamp:
                data = json.loads(report.read_text())
                self.finished = data.get("phase") == "FINISHED"
                self.final_report = data if self.finished else None
                self.report_stamp = stamp
                self.revision += 1
        except (OSError, ValueError):
            pass

        if self.final_report and previous_revision != self.revision:
            for planet in self.final_report.get("planets", []):
                station = self.stations.get(planet["station_id"])
                if station:
                    station.update(health=planet["health"], inventory=planet["inventory"],
                                   tick=self.final_report["tick"], status="finished", ended=True,
                                   offers=[], actions=[])

    def frame(self):
        return json.dumps({"revision": self.revision, "sent_ms": time.time() * 1000,
                           "finished": self.finished, "stations": self.stations}, separators=(",", ":"))


def replace_pending(queue, value):
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(value)


class Dashboard:
    def __init__(self, directory: Path, poll_ms=5, frame_ms=16):
        self.feed = Feed(directory)
        self.poll_s = poll_ms / 1000
        self.frame_s = frame_ms / 1000
        self.viewers = set()

    async def http(self, connection, request):
        if request.path == "/":
            response = connection.respond(200, PAGE.read_text())
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            response.headers["Cache-Control"] = "no-store"
            return response
        summary = re.fullmatch(r"/summary/([A-Za-z0-9_-]+)", request.path)
        if summary and summary[1] in self.feed.stations:
            path = self.feed.directory / f"{summary[1]}-summary.md"
            if path.exists():
                response = connection.respond(200, path.read_text())
                response.headers["Content-Type"] = "text/markdown; charset=utf-8"
                response.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
                return response
            return connection.respond(404, "Summary is still being written. Try again after the client exits.")
        if request.path != "/events":
            return connection.respond(404, "Not found")
        # Only this local page may subscribe from a browser.
        origin = request.headers.get("Origin")
        if origin and origin != "http://" + request.headers.get("Host", ""):
            return connection.respond(403, "Origin not allowed")

    async def viewer(self, socket):
        queue = asyncio.Queue(maxsize=1)
        self.viewers.add(queue)
        queue.put_nowait(self.feed.frame())
        async def sender():
            while True:
                frame = await queue.get()
                await asyncio.wait_for(socket.send(frame), timeout=1)

        task = asyncio.create_task(sender())
        closed = asyncio.create_task(socket.wait_closed())
        try:
            await asyncio.wait((task, closed), return_when=asyncio.FIRST_COMPLETED)
        finally:
            task.cancel()
            closed.cancel()
            await asyncio.gather(task, closed, return_exceptions=True)
            self.viewers.discard(queue)
            await socket.close()

    async def publish(self):
        revision = -1
        next_frame = 0.0
        while True:
            self.feed.poll()
            now = time.monotonic()
            if revision != self.feed.revision and now >= next_frame:
                if self.viewers:
                    frame = self.feed.frame()
                    for queue in tuple(self.viewers):
                        replace_pending(queue, frame)
                revision = self.feed.revision
                next_frame = now + self.frame_s
            await asyncio.sleep(self.poll_s)

    async def run(self, port=8766, open_browser=False, ready_file=None):
        async with serve(self.viewer, "127.0.0.1", port, process_request=self.http,
                         max_size=1024, max_queue=1, write_limit=16384) as server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            if ready_file:
                ready_file.write_text(url)
            print(f"Live dashboard: {url} (Ctrl+C to close)", flush=True)
            if open_browser:
                await asyncio.to_thread(webbrowser.open, url)
            await self.publish()


def main():
    parser = argparse.ArgumentParser(description="Live, read-only simulation dashboard")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--poll-ms", type=int, default=5)
    parser.add_argument("--frame-ms", type=int, default=16)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--ready-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.poll_ms < 1 or args.frame_ms < 1:
        parser.error("poll and frame intervals must be positive")
    try:
        asyncio.run(Dashboard(args.directory, args.poll_ms, args.frame_ms).run(
            args.port, args.open, args.ready_file))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
