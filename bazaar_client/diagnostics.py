"""Turn a failure into a category, a one-line summary and what to check next.

"Could not connect" has five very different causes. Each needs a different fix,
so every failure the client reports names its category, and the process exit
code encodes it for scripts:

  configuration   2   our settings are wrong: URL, token source, strategy name
  authentication  3   the server refused our credentials
  protocol        4   we reached a Bazaar server but disagree on the wire format
  network         5   the server could not be reached or the connection dropped
  application     1   a bug or unexpected state inside the client itself
"""

from __future__ import annotations

import asyncio
import enum
import socket
from dataclasses import dataclass

from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
)

from bazaar_client.app import SessionClosedError
from bazaar_client.codec.wire import (
    MessageTooLargeError,
    UninitializedMessageError,
    UnknownServerMessageError,
)
from bazaar_client.config import ConfigurationError
from bazaar_client.connection.lifecycle import RunIdChanged
from bazaar_client.connection.ws_client import SubprotocolNotSelected
from bazaar_client.domain.mappers import WireFormatError
from bazaar_client.domain.types import ControlCode


class FailureKind(enum.Enum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    PROTOCOL = "protocol"
    NETWORK = "network"
    APPLICATION = "application"


EXIT_CODES = {
    FailureKind.APPLICATION: 1,
    FailureKind.CONFIGURATION: 2,
    FailureKind.AUTHENTICATION: 3,
    FailureKind.PROTOCOL: 4,
    FailureKind.NETWORK: 5,
}

RETRYABLE = frozenset({FailureKind.NETWORK})


@dataclass(frozen=True, slots=True)
class Diagnosis:
    kind: FailureKind
    summary: str
    hint: str

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.kind]

    @property
    def retryable(self) -> bool:
        """Only a network failure can fix itself; the others need a person."""
        return self.kind in RETRYABLE

    def __str__(self) -> str:
        return f"[{self.kind.value}] {self.summary} -- {self.hint}"


def _describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def diagnose(exc: BaseException, control_code: ControlCode | None = None) -> Diagnosis:
    code = control_code if control_code is not None else getattr(exc, "code", None)
    text = _describe(exc)

    if isinstance(exc, ConfigurationError):
        return Diagnosis(FailureKind.CONFIGURATION, text,
                         "check --ws-url, --token/BAZAAR_TOKEN, --credentials-file and "
                         "--strategy (README: Configuration)")
    if isinstance(exc, InvalidURI):
        return Diagnosis(FailureKind.CONFIGURATION, text,
                         "the endpoint must look like ws://host:port/ws or wss://host/ws")
    if isinstance(exc, InvalidStatus):
        return _diagnose_http(exc.response.status_code, text)
    if isinstance(code, ControlCode):
        if code is ControlCode.INVALID_AUTHENTICATION:
            return Diagnosis(FailureKind.AUTHENTICATION, text,
                             "the server rejected this token for this run; fetch a current key")
        return Diagnosis(FailureKind.PROTOCOL, text,
                         f"the server refused the session with {code.name}; "
                         "check protocol version and run id")
    if isinstance(exc, (SubprotocolNotSelected, UninitializedMessageError, MessageTooLargeError,
                        WireFormatError, UnknownServerMessageError, RunIdChanged)):
        return Diagnosis(FailureKind.PROTOCOL, text,
                         "client and server disagree on the message format or run; "
                         "regenerate bazaar_pb2.py (make proto) and check the server version")
    if isinstance(exc, InvalidHandshake):
        return Diagnosis(FailureKind.PROTOCOL, text,
                         "the WebSocket handshake failed; is this a Bazaar endpoint?")
    if isinstance(exc, ConnectionRefusedError):
        return Diagnosis(FailureKind.NETWORK, text,
                         "nothing is listening there: is the server running "
                         "(docker compose up / python -m bazaar_sim.server)?")
    if isinstance(exc, socket.gaierror):
        return Diagnosis(FailureKind.NETWORK, text, "the host name did not resolve; check --ws-url")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return Diagnosis(FailureKind.NETWORK, text,
                         "no answer in time: the server is slow, unreachable, or firewalled")
    if isinstance(exc, (OSError, ConnectionClosed, SessionClosedError)):
        return Diagnosis(FailureKind.NETWORK, text,
                         "the connection dropped; the client reconnects automatically")
    return Diagnosis(FailureKind.APPLICATION, f"{type(exc).__name__}: {text}",
                     "unexpected error inside the client; rerun with --log-level DEBUG "
                     "and check the evidence log")


def _diagnose_http(status: int, text: str) -> Diagnosis:
    if status in (401, 403):
        return Diagnosis(FailureKind.AUTHENTICATION, text,
                         "token rejected: check BAZAAR_TOKEN or the credentials file; "
                         "a restarted practice server issues new keys")
    if status == 404:
        return Diagnosis(FailureKind.CONFIGURATION, text,
                         "no WebSocket endpoint at that path; the URL usually ends in /ws")
    if status == 400:
        return Diagnosis(FailureKind.PROTOCOL, text,
                         "the server refused the handshake: wrong station or subprotocol")
    if status >= 500:
        return Diagnosis(FailureKind.NETWORK, text, "the server is failing; retry later")
    return Diagnosis(FailureKind.PROTOCOL, text, f"unexpected HTTP {status} during the handshake")
