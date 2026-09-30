"""Run the upstream bridge, with optional local evidence and bounded-run exit."""

import asyncio

from bazaar_client.config import ClientConfig


def instrumented_bridge(base, evidence, finished):
    """Observe upstream traffic without changing the coordinator's decisions.

    Uses plain JSON dictionaries so our full protobuf schema is never imported
    into the process that hosts upstream's compact schema.
    """
    class Bridge(base):
        def __init__(self, *args):
            super().__init__(*args)
            self.seen_trades = set()
            self.latest = None
            self.observed_run = None

        async def _send_observation(self, hive, state):
            await super()._send_observation(hive, state)
            data = self._to_dict(state)
            if self.observed_run != data["run_id"]:
                self.seen_trades.clear()
                self.observed_run = data["run_id"]
            me = data["self"]
            sid = self.game_config.station_id
            bundle = lambda b: {r: int(b.get(r, 0)) for r in ("water", "food", "components")}
            trades = []
            for txn in data.get("transactions", {}).get("items", []):
                if txn["transaction_id"] in self.seen_trades:
                    continue
                self.seen_trades.add(txn["transaction_id"])
                outgoing = txn["proposer_id"] == sid
                trades.append({
                    "transaction_id": txn["transaction_id"], "offer_id": txn["offer_id"],
                    "counterparty": txn["recipient_id"] if outgoing else txn["proposer_id"],
                    "we_paid": bundle(txn["give"] if outgoing else txn["receive"]),
                    "we_got": bundle(txn["receive"] if outgoing else txn["give"]),
                    "settled_tick": int(txn["settled_tick"]),
                })
            offers = []
            available = bundle(me["inventory"])
            for offer in data.get("offers", {}).get("items", []):
                if offer["status"] != "OFFER_STATUS_OPEN":
                    continue
                outgoing = offer["proposer_id"] == sid
                paid = bundle(offer["give"] if outgoing else offer["receive"])
                offers.append({"offer_id": offer["offer_id"],
                    "direction": "outgoing" if outgoing else "incoming",
                    "counterparty": offer["recipient_id"] if outgoing else offer["proposer_id"],
                    "we_pay": paid, "we_get": bundle(offer["receive"] if outgoing else offer["give"]),
                    "expires_tick": int(offer["expires_tick"])})
                if outgoing:
                    available = {r: max(0, available[r] - paid[r]) for r in available}
            self.latest = dict(kind="decision", run_id=data["run_id"], tick=int(data["tick"]),
                snapshot_sequence=int(data["snapshot_sequence"]), phase=data["phase"].removeprefix("PHASE_"),
                health=int(me["health"]), inventory=bundle(me["inventory"]), available=available,
                reserve={}, open_offers=offers, open_outgoing_offers=sum(o["direction"] == "outgoing" for o in offers),
                new_transactions=trades, actions=[], reasons=["Hivemind coordinator chooses all trades."])
            if evidence:
                evidence._write_entry(dict(self.latest))
                evidence.status_event("finished" if self.latest["phase"] in ("FINISHED", "ABORTED") else
                                      "participating" if self.latest["phase"] == "RUNNING" else "waiting")
            if self.latest["phase"] in ("FINISHED", "ABORTED"):
                finished.set()

        async def _receive_hive(self, hive):
            message = await super()._receive_hive(hive)
            if evidence and self.latest and message.get("type") == "command":
                # Commands are proposals until a game_result confirms processing.
                evidence._write_entry({**self.latest, "new_transactions": [],
                    "actions": [message.get("action", {})],
                    "reasons": ["Command received from Hivemind; awaiting game result."]})
            return message

        async def _send_hive(self, hive, message):
            await super()._send_hive(hive, message)
            if evidence and message.get("type") == "game_result":
                result = message["result"]
                evidence._write_entry({"kind": "command", "action_kind": "hivemind",
                    "action": {}, "request_id": result.get("request_id"),
                    "result_ok": result.get("ok"), "result_code": result.get("code"),
                    "observed_tick": self.latest["tick"] if self.latest else None,
                    "processed_tick": int(result.get("processed_tick", 0))})
    return Bridge


async def run_hivemind_mode(config: ClientConfig) -> int:
    try:
        from spaceport_hivemind.client import HiveConfig, HivemindClient
        from spaceport_hivemind.game import GameConfig
    except ImportError as exc:
        raise RuntimeError(
            "Hivemind mode requires: git submodule update --init; "
            "python -m pip install -r requirements-hivemind.txt"
        ) from exc
    game = GameConfig(config.ws_url, config.station_id, config.token.reveal())
    hive = HiveConfig(config.hivemind_endpoint,
                      config.hivemind_key.reveal() if config.hivemind_key else None)
    if not config.evidence_file and not config.hivemind_exit_on_finish:
        await HivemindClient(game, hive).run()
        return 0
    from bazaar_client.execution.evidence import EvidenceLog
    from bazaar_client.execution.reporting import load_analyzer

    evidence = EvidenceLog(config.evidence_file) if config.evidence_file else None
    finished = asyncio.Event()
    bridge = instrumented_bridge(HivemindClient, evidence, finished)(game, hive)
    if evidence:
        evidence.run_start(station_id=config.station_id, strategy="hivemind", mode="hivemind")
    task = asyncio.create_task(bridge.run())
    stop = asyncio.create_task(finished.wait())
    try:
        if config.hivemind_exit_on_finish:
            await asyncio.wait((task, stop), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
        else:
            await task
    finally:
        task.cancel()
        stop.cancel()
        await asyncio.gather(task, stop, return_exceptions=True)
        if evidence:
            evidence.run_end()
            load_analyzer().write_reports(config.evidence_file)
    return 0
