"""Bytes on the wire: one ClientMessage out, one ServerMessage in.

Messages travel as raw binary Protobuf in a single WebSocket binary frame --
no JSON wrapper, no Base64, no length prefix.
"""

from __future__ import annotations

import bazaar_pb2

from bazaar_client.domain.mappers import (
    protocol_error_from_pb,
    readiness_from_pb,
    result_from_pb,
    snapshot_from_pb,
)
from bazaar_client.domain.types import ServerEvent


class MessageTooLargeError(ValueError):
    """Raised when an outgoing command would exceed the server's byte limit."""


class UninitializedMessageError(ValueError):
    """Raised when a message is missing required fields, before it reaches the wire."""


class UnknownServerMessageError(ValueError):
    """Raised when a ServerMessage selects no known arm of its oneof."""


def encode_client_message(
    message: bazaar_pb2.ClientMessage, max_bytes: int | None = None
) -> bytes:
    """Serialize a command, refusing to emit anything the server would reject.

    proto2 tracks required-field presence, so this catches a missing field or an
    unset nested `type` here rather than as a CONTROL_CODE_BAD_MESSAGE later.
    """
    missing = message.FindInitializationErrors()
    if missing:
        raise UninitializedMessageError(
            f"refusing to send message missing required fields: {', '.join(missing)}"
        )
    if not message.WhichOneof("message"):
        raise UninitializedMessageError("ClientMessage selected no command arm")

    payload = message.SerializeToString()
    if max_bytes is not None and len(payload) > max_bytes:
        raise MessageTooLargeError(
            f"command is {len(payload)} bytes, limit is {max_bytes}"
        )
    return payload


def parse_server_message(payload: bytes) -> bazaar_pb2.ServerMessage:
    message = bazaar_pb2.ServerMessage()
    message.ParseFromString(payload)
    return message


def dispatch_server_message(message: bazaar_pb2.ServerMessage) -> ServerEvent:
    """Resolve the ServerMessage oneof into the matching domain event."""
    arm = message.WhichOneof("message")
    if arm == "state":
        return snapshot_from_pb(message.state)
    if arm == "result":
        return result_from_pb(message.result)
    if arm == "protocol_error":
        return protocol_error_from_pb(message.protocol_error)
    if arm == "readiness":
        return readiness_from_pb(message.readiness)
    raise UnknownServerMessageError("ServerMessage selected no known arm")


def decode_server_message(payload: bytes) -> ServerEvent:
    return dispatch_server_message(parse_server_message(payload))
