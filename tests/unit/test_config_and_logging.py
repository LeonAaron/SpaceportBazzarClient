"""Endpoint/credential configuration and token redaction."""

from __future__ import annotations

import json
import logging

import pytest

from bazaar_client.config import (
    DEFAULT_WS_URL,
    MissingTokenError,
    Secret,
    config_from_args,
    read_token_from_credentials,
    resolve_token,
)
from bazaar_client.logging_setup import TokenRedactionFilter, configure_logging

TOKEN = "super-secret-token-value"


def write_credentials(tmp_path, station_id="P01", token=TOKEN):
    path = tmp_path / "validation-credentials.json"
    path.write_text(
        json.dumps({"players": [{"station_id": station_id, "token": token}]})
    )
    return path


# --- configurability ------------------------------------------------------


def test_endpoint_and_token_come_from_the_command_line(monkeypatch):
    """Changing server address or port must not require a source edit."""
    monkeypatch.delenv("BAZAAR_TOKEN", raising=False)
    config = config_from_args(
        ["--ws-url", "ws://10.0.0.5:4444/ws", "--token", TOKEN]
    )

    assert config.ws_url == "ws://10.0.0.5:4444/ws"
    assert config.token.reveal() == TOKEN


def test_secure_endpoints_are_accepted(monkeypatch):
    monkeypatch.delenv("BAZAAR_TOKEN", raising=False)
    config = config_from_args(["--ws-url", "wss://bazaar.example/ws", "--token", TOKEN])

    assert config.ws_url.startswith("wss://")


def test_endpoint_and_token_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("BAZAAR_WS_URL", "ws://env-host:3002/ws")
    monkeypatch.setenv("BAZAAR_TOKEN", "env-token")

    config = config_from_args([])

    assert config.ws_url == "ws://env-host:3002/ws"
    assert config.token.reveal() == "env-token"


def test_command_line_beats_the_environment(monkeypatch):
    monkeypatch.setenv("BAZAAR_TOKEN", "env-token")
    config = config_from_args(["--token", "cli-token"])

    assert config.token.reveal() == "cli-token"


def test_default_endpoint_matches_the_practice_server(monkeypatch):
    monkeypatch.delenv("BAZAAR_WS_URL", raising=False)
    monkeypatch.setenv("BAZAAR_TOKEN", TOKEN)

    assert config_from_args([]).ws_url == DEFAULT_WS_URL


# --- token discovery ------------------------------------------------------


def test_token_is_read_from_the_practice_credentials_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BAZAAR_TOKEN", raising=False)
    path = write_credentials(tmp_path)

    assert resolve_token(None, "P01", path).reveal() == TOKEN


def test_credentials_lookup_picks_the_requested_station(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(
        json.dumps(
            {
                "players": [
                    {"station_id": "P01", "token": "first"},
                    {"station_id": "P02", "token": "second"},
                ]
            }
        )
    )

    assert read_token_from_credentials(path, "P02") == "second"


def test_missing_station_in_credentials_is_reported(tmp_path):
    path = write_credentials(tmp_path, station_id="P01")

    with pytest.raises(MissingTokenError, match="no entry for station P09"):
        read_token_from_credentials(path, "P09")


def test_unreadable_credentials_file_is_reported(tmp_path):
    with pytest.raises(MissingTokenError, match="could not read"):
        read_token_from_credentials(tmp_path / "absent.json", "P01")


def test_no_token_anywhere_is_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.delenv("BAZAAR_TOKEN", raising=False)

    with pytest.raises(MissingTokenError, match="no token supplied"):
        resolve_token(None, "P01", tmp_path / "absent.json")


# --- the token never leaks ------------------------------------------------


def test_secret_hides_its_value_in_str_and_repr():
    secret = Secret(TOKEN)

    assert TOKEN not in str(secret)
    assert TOKEN not in repr(secret)
    assert TOKEN not in f"{secret}"
    assert secret.reveal() == TOKEN


def test_config_repr_does_not_contain_the_token(monkeypatch):
    monkeypatch.delenv("BAZAAR_TOKEN", raising=False)
    config = config_from_args(["--token", TOKEN])

    assert TOKEN not in repr(config)


def test_redaction_filter_scrubs_bearer_headers():
    scrubbed = TokenRedactionFilter().scrub(f"Authorization: Bearer {TOKEN}")

    assert TOKEN not in scrubbed
    assert "Bearer ***" in scrubbed


def test_redaction_filter_scrubs_known_secret_values():
    scrubbed = TokenRedactionFilter([TOKEN]).scrub(f"connecting with {TOKEN} now")

    assert TOKEN not in scrubbed


def test_configured_logging_redacts_a_deliberately_logged_token(caplog):
    """Even code that logs the token directly must not emit it."""
    redaction = configure_logging("INFO", secrets=[TOKEN])
    logger = logging.getLogger("bazaar_client.test")
    handler_output = []

    class Capture(logging.Handler):
        def emit(self, record):
            handler_output.append(self.format(record))

    capture = Capture()
    capture.addFilter(redaction)
    logger.addHandler(capture)
    try:
        logger.info("token is %s and header Bearer %s", TOKEN, TOKEN)
    finally:
        logger.removeHandler(capture)
        logging.getLogger().handlers.clear()

    assert handler_output
    assert all(TOKEN not in line for line in handler_output)


def test_redaction_handles_records_with_no_args():
    redaction = TokenRedactionFilter([TOKEN])
    record = logging.LogRecord(
        "n", logging.INFO, "p", 1, f"raw {TOKEN}", None, None
    )

    assert redaction.filter(record)
    assert TOKEN not in record.msg


def test_redaction_survives_a_secret_split_across_format_string_and_args():
    """"Bearer %s" plus the token only reads as a secret once interpolated.

    Rewriting the format string instead would drop a placeholder and make
    interpolation raise, so the scrub happens after the message is built.
    """
    redaction = TokenRedactionFilter([TOKEN])
    record = logging.LogRecord(
        "n", logging.INFO, "p", 1, "header Bearer %s and token %s", (TOKEN, TOKEN), None
    )

    assert redaction.filter(record)
    message = record.getMessage()  # must not raise
    assert TOKEN not in message
    assert "Bearer ***" in message


def test_redaction_leaves_ordinary_records_formattable():
    redaction = TokenRedactionFilter([TOKEN])
    record = logging.LogRecord(
        "n", logging.INFO, "p", 1, "sent %s in %d bytes", ("advertise", 42), None
    )

    assert redaction.filter(record)
    assert record.getMessage() == "sent advertise in 42 bytes"
