"""Logging with credentials scrubbed out.

The redaction filter is defence in depth: the client already passes the token
around inside `Secret`, and this catches anything that slips past it.
"""

from __future__ import annotations

import logging
import re

REDACTED = "***"
_BEARER_PATTERN = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


class TokenRedactionFilter(logging.Filter):
    """Removes bearer headers and known secret values from every record."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s]

    def add_secret(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def scrub(self, text: str) -> str:
        scrubbed = _BEARER_PATTERN.sub(rf"\1{REDACTED}", text)
        for secret in self._secrets:
            scrubbed = scrubbed.replace(secret, REDACTED)
        return scrubbed

    def filter(self, record: logging.LogRecord) -> bool:
        # Scrub the interpolated message, not the format string and args
        # separately: a secret can span both ("Bearer %s" plus the token), and
        # rewriting a format string would corrupt its placeholders.
        try:
            formatted = record.getMessage()
        except Exception:
            return True

        scrubbed = self.scrub(formatted)
        if scrubbed != formatted:
            record.msg = scrubbed
            record.args = None
        return True


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> TokenRedactionFilter:
    """Install console logging with redaction. Returns the filter for later secrets."""
    redaction = TokenRedactionFilter(secrets)

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    handler.addFilter(redaction)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.addFilter(redaction)

    return redaction
