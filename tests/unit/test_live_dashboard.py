import asyncio
import json
import time

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from bazaar_client.live_dashboard import Dashboard, Feed, replace_pending


def decision(tick):
    return {"kind": "decision", "tick": tick, "health": 100,
            "inventory": {"water": tick, "food": 30, "components": 30},
            "actions": [], "reasons": ["Waiting"], "new_transactions": []}


def test_tail_handles_partial_lines_rotation_and_bounded_history(tmp_path):
    path = tmp_path / "P01-evidence.jsonl"
    path.write_text(json.dumps(decision(0))[:-1])
    feed = Feed(tmp_path)
    feed.poll()
    assert not feed.stations
    with path.open("a") as f:
        f.write("}\n")
    feed.poll()
    assert feed.stations["P01"]["tick"] == 0
    with path.open("a") as f:
        for tick in range(1, 201):
            f.write(json.dumps(decision(tick)) + "\n")
    feed.poll()
    assert len(feed.stations["P01"]["history"]) == 120
    assert feed.stations["P01"]["tick"] == 200
    revision = feed.revision
    feed.poll()
    assert feed.revision == revision
    path.rename(tmp_path / "P01-evidence.old.jsonl")
    path.write_text(json.dumps(decision(1)) + "\n")
    feed.poll()
    assert feed.stations["P01"]["tick"] == 1
    assert len(feed.stations["P01"]["history"]) == 1


def test_final_server_state_wins_over_late_client_decisions(tmp_path):
    path = tmp_path / "P01-evidence.jsonl"
    path.write_text(json.dumps(decision(9)) + "\n")
    report = {"phase": "FINISHED", "tick": 10, "planets": [
        {"station_id": "P01", "health": 95, "inventory": {"water": 8}}]}
    (tmp_path / "server-report.json").write_text(json.dumps(report))
    feed = Feed(tmp_path)
    feed.poll()
    with path.open("a") as f:
        f.write(json.dumps(decision(9)) + "\n")
    feed.poll()
    assert feed.stations["P01"]["tick"] == 10
    assert feed.stations["P01"]["health"] == 95
    assert feed.finished


def test_slow_viewer_retains_only_the_latest_frame():
    queue = asyncio.Queue(maxsize=1)
    for i in range(10000):
        replace_pending(queue, i)
    assert queue.qsize() == 1
    assert queue.get_nowait() == 9999


async def test_socket_streams_new_log_bytes_and_reconnects(tmp_path):
    dashboard = Dashboard(tmp_path)
    path = tmp_path / "P01-evidence.jsonl"
    path.write_text(json.dumps(decision(0)) + "\n")
    async with serve(dashboard.viewer, "127.0.0.1", 0, process_request=dashboard.http) as server:
        port = server.sockets[0].getsockname()[1]
        producer = asyncio.create_task(dashboard.publish())
        try:
            async with connect(f"ws://127.0.0.1:{port}/events") as ws:
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 1))
                    if frame["stations"]:
                        break
                started = time.perf_counter()
                with path.open("a") as f:
                    f.write(json.dumps(decision(1)) + "\n")
                frame = json.loads(await asyncio.wait_for(ws.recv(), 1))
                assert frame["stations"]["P01"]["tick"] == 1
                # Generous regression ceiling; actual local timing is measured separately.
                assert time.perf_counter() - started < .5
            async with connect(f"ws://127.0.0.1:{port}/events") as ws:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 1))
                assert frame["stations"]["P01"]["tick"] == 1
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            response = await reader.read()
            assert b"200 OK" in response and b"Fleet overview" in response
            writer.close()
            await writer.wait_closed()
            (tmp_path / "P01-summary.md").write_text("# P01 summary\n")
            for route, expected in (("/summary/P01", b"# P01 summary"),
                                    ("/credentials.json", b"404 Not Found")):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(f"GET {route} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
                await writer.drain()
                assert expected in await reader.read()
                writer.close()
                await writer.wait_closed()
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
