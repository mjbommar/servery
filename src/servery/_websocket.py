"""WebSocket (RFC 6455) server framing + the ASGI ``websocket`` protocol.

Driven by :mod:`servery.asgi`: when a request arrives with ``Upgrade: websocket``,
the parsed head is handed here. We build the ASGI ``websocket`` scope and run the
app with ``receive``/``send`` that speak the RFC 6455 wire protocol — the opening
handshake (101 + ``Sec-WebSocket-Accept``), masked client frames, fragmentation,
and the ping/pong/close control frames.

Single-coroutine model: ``receive()`` reads the next data message (transparently
answering pings and handling close), ``send()`` writes a frame. That covers
request/response and echo apps; it does not run independent concurrent reader and
writer tasks, so an app that pushes unsolicited frames while also blocked in
``receive()`` is not supported (documented limitation).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from typing import Any

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 §1.3
_MAX_PAYLOAD = 16 * 1024 * 1024  # per-message cap (DoS guard)

# Opcodes (RFC 6455 §5.2)
_CONT, _TEXT, _BINARY, _CLOSE, _PING, _PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class _ClosedError(Exception):
    """The peer sent a close frame (or the transport died)."""

    def __init__(self, code: int = 1006) -> None:
        super().__init__(code)
        self.code = code


def accept_key(key: bytes) -> str:
    """Compute the ``Sec-WebSocket-Accept`` value for a client key (RFC 6455 §4.2.2)."""
    digest = hashlib.sha1(key + _GUID).digest()  # nosec B324 (RFC 6455 handshake, not security)
    return base64.b64encode(digest).decode("ascii")


def _frame(opcode: int, payload: bytes) -> bytes:
    """Encode a server->client frame (FIN set, never masked)."""
    n = len(payload)
    if n < 126:
        header = bytes((0x80 | opcode, n))
    elif n < 65536:
        header = bytes((0x80 | opcode, 126)) + struct.pack("!H", n)
    else:
        header = bytes((0x80 | opcode, 127)) + struct.pack("!Q", n)
    return header + payload


async def _read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    b0, b1 = await reader.readexactly(2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    if length > _MAX_PAYLOAD:
        raise _ClosedError(1009)  # message too big
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length)
    if masked:  # client frames MUST be masked (RFC 6455 §5.1)
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return fin, opcode, payload


async def _read_message(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[int, bytes]:
    """Read one full application message, answering ping/close along the way."""
    fragments: list[bytes] = []
    total = 0
    msg_opcode = _TEXT
    while True:
        fin, opcode, payload = await _read_frame(reader)
        if opcode == _CLOSE:
            code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
            writer.write(_frame(_CLOSE, payload[:2]))  # echo the close
            await writer.drain()
            raise _ClosedError(code)
        if opcode == _PING:
            writer.write(_frame(_PONG, payload))
            await writer.drain()
            continue
        if opcode == _PONG:
            continue
        if opcode != _CONT:  # start of a new data message
            msg_opcode = opcode
        fragments.append(payload)
        total += len(payload)
        if total > _MAX_PAYLOAD:
            raise _ClosedError(1009)
        if fin:
            return msg_opcode, b"".join(fragments)


async def _send_handshake(
    writer: asyncio.StreamWriter, key: bytes, subprotocol: str | None, extra: list[Any]
) -> None:
    lines = [
        b"HTTP/1.1 101 Switching Protocols",
        b"Upgrade: websocket",
        b"Connection: Upgrade",
        b"Sec-WebSocket-Accept: " + accept_key(key).encode("ascii"),
    ]
    if subprotocol:
        lines.append(b"Sec-WebSocket-Protocol: " + subprotocol.encode("ascii"))
    for name, value in extra:  # app-supplied response headers
        lines.append(bytes(name) + b": " + bytes(value))
    writer.write(b"\r\n".join(lines) + b"\r\n\r\n")
    await writer.drain()


async def serve(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    scope: dict[str, Any],
    app: Any,
    key: bytes,
) -> None:
    """Run the ASGI ``websocket`` app over this connection."""
    state = {"connect_sent": False, "accepted": False, "closed": False}

    async def receive() -> dict[str, Any]:
        if not state["connect_sent"]:
            state["connect_sent"] = True
            return {"type": "websocket.connect"}
        if state["closed"]:
            return {"type": "websocket.disconnect", "code": 1005}
        try:
            opcode, data = await _read_message(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            state["closed"] = True
            return {"type": "websocket.disconnect", "code": getattr(exc, "code", 1006)}
        except _ClosedError as exc:
            state["closed"] = True
            return {"type": "websocket.disconnect", "code": exc.code}
        if opcode == _TEXT:
            return {"type": "websocket.receive", "text": data.decode("utf-8", "replace")}
        return {"type": "websocket.receive", "bytes": data}

    async def send(event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "websocket.accept":
            await _send_handshake(writer, key, event.get("subprotocol"), event.get("headers", []))
            state["accepted"] = True
        elif kind == "websocket.send" and not state["closed"]:
            if event.get("text") is not None:
                writer.write(_frame(_TEXT, event["text"].encode("utf-8")))
            elif event.get("bytes") is not None:
                writer.write(_frame(_BINARY, event["bytes"]))
            await writer.drain()
        elif kind == "websocket.close":
            if not state["accepted"]:  # rejected before the handshake
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            elif not state["closed"]:
                writer.write(_frame(_CLOSE, struct.pack("!H", event.get("code", 1000))))
            await writer.drain()
            state["closed"] = True

    await app(scope, receive, send)
