"""WebSocket transport.

One ClientMessage per binary frame, one ServerMessage per incoming frame. The
receive loop only decodes and enqueues: it never calls decision code, so the
client keeps reading while it thinks.
"""

from __future__ import annotations

import asyncio
import logging

import websockets
from websockets.asyncio.client import ClientConnection

from bazaar_client.codec.wire import decode_server_message, encode_client_message
from bazaar_client.config import SUBPROTOCOL, ClientConfig
from bazaar_client.domain.types import ServerEvent

logger = logging.getLogger(__name__)


class SubprotocolNotSelected(RuntimeError):
    """Raised when the server did not confirm the protobuf subprotocol."""


class NotConnectedError(RuntimeError):
    pass


class BazaarConnection:
    """Owns the socket. Knows nothing about game rules."""

    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._ws: ClientConnection | None = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> None:
        logger.info("connecting to %s as %s", self._config.ws_url, self._config.station_id)
        self._ws = await websockets.connect(
            self._config.ws_url,
            additional_headers={
                "Authorization": f"Bearer {self._config.token.reveal()}"
            },
            subprotocols=[SUBPROTOCOL],
            open_timeout=self._config.connect_timeout_s,
        )

        selected = self._ws.subprotocol
        if selected != SUBPROTOCOL:
            await self.close()
            raise SubprotocolNotSelected(
                f"server selected subprotocol {selected!r}, expected {SUBPROTOCOL!r}"
            )
        logger.info("connected; server confirmed subprotocol %s", selected)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def send(self, message, max_bytes: int | None = None) -> bytes:
        """Send one ClientMessage as binary protobuf. Returns the bytes sent."""
        return await self.send_payload(
            encode_client_message(message, max_bytes=max_bytes)
        )

    async def send_payload(self, payload: bytes) -> bytes:
        """Send already-encoded bytes, for callers that must inspect them first."""
        if self._ws is None:
            raise NotConnectedError("send() before connect()")
        await self._ws.send(payload)
        return payload

    async def recv_loop(self, queue: asyncio.Queue[ServerEvent | BaseException]) -> None:
        """Drain the socket into a queue until it closes.

        Decoding failures are posted to the queue rather than raised, so the
        consumer decides what to do and the loop keeps its single owner.
        """
        if self._ws is None:
            raise NotConnectedError("recv_loop() before connect()")
        try:
            async for raw in self._ws:
                if isinstance(raw, str):
                    logger.warning("ignoring unexpected text frame of %d chars", len(raw))
                    continue
                try:
                    await queue.put(decode_server_message(raw))
                except Exception as exc:  # malformed or unknown message
                    logger.error("could not decode a server message: %s", exc)
                    await queue.put(exc)
        except websockets.ConnectionClosed as exc:
            logger.info("connection closed: %s", exc)
        finally:
            await queue.put(ConnectionClosedSentinel())


class ConnectionClosedSentinel(BaseException):
    """Posted to the event queue when the receive loop ends."""
