# Proposed structured log format (Task 7)

A client writes one JSON object per line (JSONL) to a file, appending and
closing each line as it happens so the log survives a crash. Any language can
produce it; `scripts/analyze_evidence.py` (Python standard library only) turns
it into a text summary, an interactive HTML report, or one offer's full story.
Fields a client does not have can be left out: the summary uses what is there.

## Record kinds

Every record has `kind` (except command records, recognised by `action_kind`)
and an ISO-8601 UTC `timestamp`.

| kind | When | Key fields |
|---|---|---|
| `run_start` | once, at process start | `build`, `dirty`, `strategy`, `mode`, `station_id`, `ws_url`, `python` |
| `status` | the client's state changes | `status` (starting, connecting, connected, authenticated, synchronized, waiting, participating, stale, finished, disconnected), `previous`, `detail` |
| `connection` | the socket changes | `event` (connecting, connected, disconnected, reconnecting, closed, failed), `detail`, `category` |
| `decision` | at least once per tick | `decision_id`, `run_id`, `tick`, `phase`, stimuli (`health`, `inventory`, `open_offers`, `new_transactions`, `import_targets`, `specialty_spendable`), the choice (`verdict` act/wait, `actions`, `reasons`, `wait_reason`, `passed_offers`), `timing` |
| command (`action_kind`) | per command sent | `decision_id`, `request_id`, `action`, `observed_tick`, `result_ok`, `result_code`, `object_id`, `transaction_id`, `processed_tick`, `deadline_missed`, `response_ms`, `confirm_ms`, `inventory_after` |
| `run_end` | once, at exit | counts, `latency` (p50/p95/max per stage), `missed_deadlines`, `status_seconds`, `final_status` |

Offers and trades inside a decision are written **from our side**:
`we_pay` / `we_get`, `counterparty`, `direction` (incoming/outgoing).

## Identifiers that connect events

`decision_id` joins a decision to the commands it caused; `request_id` joins a
command to its result; `object_id` joins a command to the offer or
advertisement it created; `transaction_id` and `offer_id` join an offer to the
trade it became. `run_id` and `session` separate runs and reconnections.

## Example (one incoming offer, stimuli -> decision -> outcome)

```json
{"kind": "decision", "timestamp": "2026-09-28T18:12:33.391+00:00", "decision_id": "d25", "run_id": "sim-b819", "tick": 14, "phase": "RUNNING", "health": 100, "inventory": {"water": 52, "food": 19, "components": 20}, "open_offers": [{"offer_id": "offer-37", "direction": "incoming", "counterparty": "P03", "we_pay": {"water": 1, "food": 0, "components": 0}, "we_get": {"water": 0, "food": 0, "components": 1}, "expires_tick": 19}], "verdict": "act", "actions": [{"kind": "accept", "offer_id": "offer-37"}], "reasons": ["accept offer-37: our specialty for what we import, at least 1:1"], "timing": {"queue_ms": 0.4, "decide_ms": 0.9}}
{"step": "tick-14", "action_kind": "accept", "action": {"offer_id": "offer-37"}, "timestamp": "2026-09-28T18:12:33.392+00:00", "decision_id": "d25", "request_id": "4104887fd2ac-accept-P01-14", "observed_tick": 14, "result_ok": true, "result_code": "OK", "object_id": "offer-37", "transaction_id": "txn-44", "processed_tick": 14, "deadline_missed": false, "response_ms": 3.1, "confirm_ms": 2.9, "inventory_after": {"water": 49, "food": 21, "components": 21}}
```

## Summaries generated from it

```sh
python scripts/analyze_evidence.py run.jsonl                    # text run summary
python scripts/analyze_evidence.py run.jsonl --html report.html # interactive dashboard
python scripts/analyze_evidence.py run.jsonl --offer offer-37   # one offer, end to end
```

The summary covers resource and health history, completed trades, rejected
and unanswered commands, disconnected periods and decision gaps, shortages,
responsiveness, acting versus waiting, and the stimuli -> decision -> outcome
trace per tick. The reference producer is `bazaar_client/execution/evidence.py`.
