"""The server side of the wire: ClientMessage in, ServerMessage out.

The only module in `bazaar_sim` that imports the generated protobuf, the same
boundary `bazaar_client` keeps (enforced by tests/unit/test_layering.py).
Server messages are built with the client's own `*_to_pb` mappers, so both
ends agree on every required field, zero and nullable arm by construction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import bazaar_pb2
from google.protobuf.message import DecodeError

from bazaar_client.domain import mappers
from bazaar_client.domain.types import (
    CommandResult,
    ControlCode,
    ProtocolErrorEvent,
    ReadinessAck,
    Resource,
    Snapshot,
)
from bazaar_client.execution.actions import (
    AcceptAction,
    Action,
    AdvertiseAction,
    OfferAction,
    WithdrawAction,
)

COMMAND_ARMS = ("advertise", "offer", "accept", "withdraw")


class BadMessage(ValueError):
    """The bytes are not a well-formed ClientMessage: answered with BAD_MESSAGE."""

    def __init__(self, reason: str, request_id: str | None = None) -> None:
        super().__init__(reason)
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class ClientCommand:
    kind: str
    protocol_version: str
    run_id: str
    request_id: str | None = None
    action: Action | None = None
    ready: bool | None = None
    snapshot_sequence: int | None = None
    fingerprint: str = ""


def _resources(pb: bazaar_pb2.ListResource) -> frozenset[Resource]:
    try:
        return frozenset(Resource(item) for item in pb.items)
    except ValueError as exc:
        raise BadMessage(f"unknown resource: {exc}") from exc


def decode_client_message(payload: bytes) -> ClientCommand:
    message = bazaar_pb2.ClientMessage()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise BadMessage(f"not a ClientMessage: {exc}") from exc
    arm = message.WhichOneof("message")
    if arm is None:
        raise BadMessage("ClientMessage selected no arm")
    body = getattr(message, arm)
    request_id = getattr(body, "request_id", None) if arm in COMMAND_ARMS else None
    missing = message.FindInitializationErrors()
    if missing:
        raise BadMessage(f"missing required fields: {', '.join(missing)}", request_id)

    common = dict(kind=arm, protocol_version=body.protocol_version, run_id=body.run_id)
    if arm == "ready":
        return ClientCommand(**common, ready=body.ready, snapshot_sequence=body.snapshot_sequence)
    if arm == "sync":
        return ClientCommand(**common)

    # An exact retry must be recognisable, so identical bodies share a fingerprint.
    fingerprint = hashlib.sha256(body.body.SerializeToString(deterministic=True)).hexdigest()
    if arm == "advertise":
        action = AdvertiseAction(
            _resources(body.body.selling), _resources(body.body.seeking), body.body.expires_tick
        )
    elif arm == "offer":
        action = OfferAction(
            body.body.recipient_id,
            mappers.bundle_from_pb(body.body.give),
            mappers.bundle_from_pb(body.body.receive),
            body.body.expires_tick,
        )
    elif arm == "accept":
        action = AcceptAction(body.body.offer_id)
    else:
        action = WithdrawAction(body.body.object_id)
    return ClientCommand(**common, request_id=request_id, action=action, fingerprint=fingerprint)


def encode_state(snapshot: Snapshot) -> bytes:
    return bazaar_pb2.ServerMessage(state=mappers.snapshot_to_pb(snapshot)).SerializeToString()


def encode_result(result: CommandResult, run_id: str) -> bytes:
    return bazaar_pb2.ServerMessage(result=mappers.result_to_pb(result, run_id)).SerializeToString()


def encode_readiness(ack: ReadinessAck) -> bytes:
    return bazaar_pb2.ServerMessage(readiness=mappers.readiness_to_pb(ack)).SerializeToString()


def encode_protocol_error(
    code: ControlCode, *, run_id: str | None, request_id: str | None, close_session: bool
) -> bytes:
    event = ProtocolErrorEvent(run_id=run_id, request_id=request_id, code=code,
                               close_session=close_session)
    return bazaar_pb2.ServerMessage(
        protocol_error=mappers.protocol_error_to_pb(event)
    ).SerializeToString()
