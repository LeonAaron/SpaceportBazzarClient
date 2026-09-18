"""Runtime configuration: endpoint and credentials, never source constants.

The endpoint and token are settable without editing or recompiling anything, and
the token is wrapped so it cannot be printed by accident.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

SUBPROTOCOL = "bazaar.protobuf.v2"
DEFAULT_WS_URL = "ws://127.0.0.1:3001/ws"
DEFAULT_CREDENTIALS_FILE = Path("validation-credentials.json")
DEFAULT_STATION_ID = "P01"


class MissingTokenError(RuntimeError):
    """Raised when no token was supplied and none could be discovered."""


class Secret:
    """Holds a credential whose value never appears in logs or tracebacks.

    `reveal()` is deliberately the only way out, so the handful of places that
    legitimately need the raw value are greppable.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True, slots=True)
class ClientConfig:
    ws_url: str
    token: Secret
    station_id: str = DEFAULT_STATION_ID
    run_id_file: Path | None = None
    log_level: str = "INFO"
    connect_timeout_s: float = 10.0
    reconnect_max_backoff_s: float = 30.0


def read_token_from_credentials(path: Path, station_id: str) -> str:
    """Pull a station's token out of the practice server's credentials file."""
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MissingTokenError(f"could not read credentials file {path}: {exc}") from exc

    for player in document.get("players", []):
        if player.get("station_id") == station_id:
            token = player.get("token")
            if token:
                return token
            raise MissingTokenError(f"{path} has no token for station {station_id}")

    raise MissingTokenError(f"{path} has no entry for station {station_id}")


def resolve_token(
    cli_token: str | None, station_id: str, credentials_file: Path
) -> Secret:
    """CLI flag, then environment, then the practice server's credentials file."""
    if cli_token:
        return Secret(cli_token)

    env_token = os.environ.get("BAZAAR_TOKEN")
    if env_token:
        return Secret(env_token)

    if credentials_file.exists():
        return Secret(read_token_from_credentials(credentials_file, station_id))

    raise MissingTokenError(
        "no token supplied: pass --token, set BAZAAR_TOKEN, or run where "
        f"{credentials_file} exists"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bazaar-client", description="Spaceport Bazaar trading client"
    )
    parser.add_argument(
        "--ws-url",
        default=os.environ.get("BAZAAR_WS_URL", DEFAULT_WS_URL),
        help="full websocket endpoint, ws:// or wss:// (env: BAZAAR_WS_URL)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="access token (env: BAZAAR_TOKEN; falls back to the credentials file)",
    )
    parser.add_argument(
        "--station-id",
        default=os.environ.get("BAZAAR_STATION_ID", DEFAULT_STATION_ID),
        help="which station's token to read from the credentials file",
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=Path(os.environ.get("BAZAAR_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_FILE)),
        help="practice-server credentials file used when no token is given",
    )
    parser.add_argument(
        "--run-id-file",
        type=Path,
        default=(
            Path(os.environ["BAZAAR_RUN_ID_FILE"])
            if os.environ.get("BAZAAR_RUN_ID_FILE")
            else None
        ),
        help="optional path to persist the discovered run id",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("BAZAAR_LOG_LEVEL", "INFO"),
        help="logging level (env: BAZAAR_LOG_LEVEL)",
    )
    return parser


def config_from_args(argv: list[str] | None = None) -> ClientConfig:
    args = build_parser().parse_args(argv)
    return ClientConfig(
        ws_url=args.ws_url,
        token=resolve_token(args.token, args.station_id, args.credentials_file),
        station_id=args.station_id,
        run_id_file=args.run_id_file,
        log_level=args.log_level,
    )
