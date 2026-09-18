"""Entrypoint for the step-2 handshake check.

Connects, reads the opening state, completes the readiness exchange and sends a
single advertise command, reporting what the server answered at each point.
Later steps replace this with the scripted driver and then the trading policy.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from bazaar_client.app import BazaarSession, CommandOutcome
from bazaar_client.config import ClientConfig, MissingTokenError, config_from_args
from bazaar_client.domain import mappers
from bazaar_client.domain.types import Phase, Resource, Snapshot
from bazaar_client.logging_setup import configure_logging

logger = logging.getLogger("bazaar_client.cli")


def describe_opening_state(snapshot: Snapshot) -> None:
    station = snapshot.me
    logger.info(
        "station=%s phase=%s tick=%d world_version=%d snapshot_sequence=%d",
        snapshot.self_station_id,
        snapshot.phase.name,
        snapshot.tick,
        snapshot.world_version,
        snapshot.snapshot_sequence,
    )
    logger.info(
        "inventory water=%d food=%d components=%d | health=%d | specialty=%s",
        station.inventory.water,
        station.inventory.food,
        station.inventory.components,
        station.health,
        station.specialty.name,
    )
    logger.info(
        "upkeep per tick water=%d food=%d components=%d | last production %s",
        station.upkeep_per_tick.water,
        station.upkeep_per_tick.food,
        station.upkeep_per_tick.components,
        station.last_production.as_dict(),
    )
    logger.info(
        "rules: max_health=%d damage_per_unit=%d recovery=%d commands_per_tick=%d "
        "max_offer_ttl=%d max_ad_ttl=%d",
        snapshot.rules.max_health,
        snapshot.rules.shortage_damage_per_unit,
        snapshot.rules.recovery_per_fully_supplied_tick,
        snapshot.rules.new_commands_per_station_per_tick,
        snapshot.rules.max_offer_ttl_ticks,
        snapshot.rules.max_publication_ttl_ticks,
    )
    logger.info(
        "directory: %s",
        ", ".join(f"{e.station_id}={e.display_name}" for e in snapshot.directory),
    )
    for ad in snapshot.advertisements:
        logger.info(
            "advertisement %s from %s selling=%s seeking=%s expires_tick=%d",
            ad.advertisement_id,
            ad.station_id,
            sorted(r.name for r in ad.selling),
            sorted(r.name for r in ad.seeking),
            ad.expires_tick,
        )


def describe_outcome(outcome: CommandOutcome) -> None:
    if outcome.result is not None:
        logger.info(
            "result request_id=%s ok=%s code=%s object_id=%s processed_tick=%d "
            "processed_version=%d",
            outcome.result.request_id,
            outcome.result.ok,
            outcome.result.code.name,
            outcome.result.object_id,
            outcome.result.processed_tick,
            outcome.result.processed_version,
        )
    if outcome.error is not None:
        logger.warning(
            "protocol_error request_id=%s code=%s close_session=%s",
            outcome.error.request_id,
            outcome.error.code.name,
            outcome.error.close_session,
        )


async def run(config: ClientConfig) -> int:
    async with BazaarSession(config) as session:
        snapshot, ack = await session.handshake()

        logger.info("--- opening state ---")
        describe_opening_state(snapshot)

        logger.info("--- readiness ---")
        logger.info(
            "readiness confirmed run_id=%s ready=%s snapshot_sequence=%d",
            ack.run_id,
            ack.ready,
            ack.snapshot_sequence,
        )
        if not (ack.ready and ack.snapshot_sequence == snapshot.snapshot_sequence):
            logger.error("readiness did not match the snapshot we declared")
            return 1

        if config.run_id_file is not None:
            config.run_id_file.write_text(snapshot.run_id)
            logger.info("recorded run id in %s", config.run_id_file)

        if not session.lifecycle.can_send_trading_commands():
            logger.info(
                "phase is %s, so no trading command is sent; readiness is complete",
                snapshot.phase.name if snapshot.phase else "unknown",
            )
            return 0

        logger.info("--- one advertise command ---")
        request_id = session.request_ids.next("advertise")
        expires_tick = snapshot.tick + min(6, snapshot.rules.max_publication_ttl_ticks)
        message = mappers.build_advertise(
            session.run_id,
            request_id,
            selling=[Resource.WATER],
            seeking=[Resource.FOOD],
            expires_tick=expires_tick,
        )

        outcome = await session.send_command(message, kind="advertise", request_id=request_id)
        describe_outcome(outcome)
        if not outcome.ok:
            logger.error("advertise was not accepted: %s", outcome.code_name)
            return 1

        follow_up = await session.wait_for_snapshot(
            min_sequence=snapshot.snapshot_sequence + 1
        )
        logger.info(
            "follow-up state snapshot_sequence=%d world_version=%d",
            follow_up.snapshot_sequence,
            follow_up.world_version,
        )
        own = follow_up.own_advertisement()
        if own is None:
            logger.error("our advertisement is missing from the new state")
            return 1
        logger.info(
            "our advertisement %s selling=%s seeking=%s; inventory unchanged: %s",
            own.advertisement_id,
            sorted(r.name for r in own.selling),
            sorted(r.name for r in own.seeking),
            follow_up.me.inventory.as_dict(),
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    try:
        config = config_from_args(argv)
    except MissingTokenError as exc:
        # Before logging is configured, so write plainly rather than traceback.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level, secrets=[config.token.reveal()])
    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.error("client failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
