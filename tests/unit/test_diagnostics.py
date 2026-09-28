"""Every failure names its category, a hint, and an exit code scripts can test."""

from __future__ import annotations

import asyncio
import socket

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus, InvalidURI
from websockets.http11 import Response

from bazaar_client.app import SessionAbortedError, SessionClosedError
from bazaar_client.codec.wire import UninitializedMessageError
from bazaar_client.config import ConfigurationError, MissingTokenError
from bazaar_client.connection.ws_client import SubprotocolNotSelected
from bazaar_client.diagnostics import EXIT_CODES, FailureKind, diagnose
from bazaar_client.domain.types import ControlCode


def http(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "reason", Headers(), b""))


@pytest.mark.parametrize("error, kind", [
    (MissingTokenError("no token"), FailureKind.CONFIGURATION),
    (ConfigurationError("unknown strategy"), FailureKind.CONFIGURATION),
    (InvalidURI("nonsense", "not a ws url"), FailureKind.CONFIGURATION),
    (http(404), FailureKind.CONFIGURATION),
    (http(401), FailureKind.AUTHENTICATION),
    (http(403), FailureKind.AUTHENTICATION),
    (SessionAbortedError("fatal", ControlCode.INVALID_AUTHENTICATION), FailureKind.AUTHENTICATION),
    (http(400), FailureKind.PROTOCOL),
    (SubprotocolNotSelected("json"), FailureKind.PROTOCOL),
    (UninitializedMessageError("missing"), FailureKind.PROTOCOL),
    (SessionAbortedError("fatal", ControlCode.UNSUPPORTED_VERSION), FailureKind.PROTOCOL),
    (ConnectionRefusedError("refused"), FailureKind.NETWORK),
    (socket.gaierror("no such host"), FailureKind.NETWORK),
    (asyncio.TimeoutError(), FailureKind.NETWORK),
    (SessionClosedError("connection closed"), FailureKind.NETWORK),
    (http(503), FailureKind.NETWORK),
    (KeyError("bug"), FailureKind.APPLICATION),
], ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else value.value)
def test_each_failure_lands_in_its_category(error, kind):
    diagnosis = diagnose(error)

    assert diagnosis.kind is kind
    assert diagnosis.hint and diagnosis.summary


def test_exit_codes_are_distinct_per_category():
    assert len(set(EXIT_CODES.values())) == len(FailureKind)
    assert diagnose(http(401)).exit_code == 3
    assert diagnose(ConnectionRefusedError()).exit_code == 5


def test_only_network_failures_are_worth_retrying():
    assert diagnose(ConnectionRefusedError()).retryable
    assert not diagnose(http(401)).retryable
    assert not diagnose(SubprotocolNotSelected("x")).retryable
    assert not diagnose(KeyError("bug")).retryable


def test_a_diagnosis_reads_as_one_line_with_its_category():
    text = str(diagnose(ConnectionRefusedError("refused")))

    assert text.startswith("[network] refused -- ")
    assert "\n" not in text
