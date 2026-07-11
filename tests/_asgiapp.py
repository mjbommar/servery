"""ASGI app fixtures for tests/test_asgi.py (not collected by ``unittest``)."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any

_lifespan_log: list[str] = []
write_timeout_finished = threading.Event()
peer_disconnect_received = threading.Event()
post_response_disconnect_received = threading.Event()
closed_send_error_received = threading.Event()


async def echo(scope: dict[str, Any], receive: Any, send: Any) -> None:
    assert scope["type"] == "http"
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    reply = b" ".join([b"asgi", scope["method"].encode(), scope["path"].encode(), b"".join(chunks)])
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": reply})


async def streaming(scope: dict[str, Any], receive: Any, send: Any) -> None:
    # Two body events, no Content-Length -> exercises chunked transfer-encoding.
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"part1", "more_body": True})
    await send({"type": "http.response.body", "body": b"part2", "more_body": False})


async def write_until_blocked(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Allocate a long stream so a non-reading peer eventually stalls drain()."""
    await receive()
    chunk_size = 64 * 1024
    count = 16 * 1024
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(chunk_size * count).encode())],
        }
    )
    try:
        for index in range(count):
            body = index.to_bytes(8, "big") + b"x" * (chunk_size - 8)
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                    "more_body": index < count - 1,
                }
            )
    finally:
        write_timeout_finished.set()


async def body_shape(scope: dict[str, Any], receive: Any, send: Any) -> None:
    sizes: list[int] = []
    while True:
        message = await receive()
        sizes.append(len(message.get("body", b"")))
        if not message.get("more_body", False):
            break
    nonempty = [size for size in sizes if size]
    payload = f"{len(nonempty)}:{max(nonempty, default=0)}:{sum(nonempty)}".encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(payload)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def ignores_body(scope: dict[str, Any], receive: Any, send: Any) -> None:
    payload = b"ignored"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(payload)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def ws_echo(scope: dict[str, Any], receive: Any, send: Any) -> None:
    assert scope["type"] == "websocket"
    await receive()  # websocket.connect
    await send({"type": "websocket.accept"})
    while True:
        event = await receive()
        if event["type"] == "websocket.disconnect":
            return
        if event["type"] == "websocket.receive":
            await send({"type": "websocket.send", "text": "echo:" + (event.get("text") or "")})


async def crashing(scope: dict[str, Any], receive: Any, send: Any) -> None:
    # Raises out of the app without sending a response — server must 500 + log.
    await receive()
    raise RuntimeError("boom in the app")


async def wait_for_peer_disconnect(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Wait after the final request event until the client actually closes."""
    if scope["type"] == "lifespan":
        raise RuntimeError("lifespan unsupported")
    first = await receive()
    assert first == {"type": "http.request", "body": b"", "more_body": False}
    event = await receive()
    if event == {"type": "http.disconnect"}:
        peer_disconnect_received.set()


async def wait_after_response(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """A completed response makes the request scope receive-side disconnected."""
    if scope["type"] == "lifespan":
        raise RuntimeError("lifespan unsupported")
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})
    if await receive() == {"type": "http.disconnect"}:
        post_response_disconnect_received.set()


async def send_after_response(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Catch the ASGI 2.4 OSError raised after the final response event."""
    if scope["type"] == "lifespan":
        raise RuntimeError("lifespan unsupported")
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})
    try:
        await send({"type": "http.response.body", "body": b"late"})
    except OSError:
        closed_send_error_received.set()


async def uncaught_send_after_response(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Let the server consume a post-final OSError without losing pipeline reuse."""
    if scope["type"] == "lifespan":
        raise RuntimeError("lifespan unsupported")
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})
    await send({"type": "http.response.body", "body": b"late"})


async def response_trailers(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Emit two trailer events; the server negotiates whether they reach the wire."""
    if scope["type"] == "lifespan":
        raise RuntimeError("lifespan unsupported")
    assert "http.response.trailers" in scope["extensions"]
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"4"), (b"trailer", b"x-one, x-two")],
            "trailers": True,
        }
    )
    await send({"type": "http.response.body", "body": b"data"})
    await send(
        {
            "type": "http.response.trailers",
            "headers": [(b"x-one", b"a")],
            "more_trailers": True,
        }
    )
    await send({"type": "http.response.trailers", "headers": [(b"x-two", b"b")]})


async def incomplete_response(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Return after a buffered start event without the required body event."""
    if scope["type"] == "lifespan":
        raise RuntimeError("lifespan unsupported")
    await receive()
    await send({"type": "http.response.start", "status": 200})


async def cancel_disconnect_listener(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Cancel a listener, then respond so the HTTP/1 connection remains reusable."""
    if scope["type"] == "lifespan":
        raise RuntimeError("lifespan unsupported")
    await receive()
    listener = asyncio.create_task(receive())
    await asyncio.sleep(0)
    listener.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await listener
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def with_lifespan(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                _lifespan_log.append("startup")
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                _lifespan_log.append("shutdown")
                await send({"type": "lifespan.shutdown.complete"})
                return
    else:
        await receive()
        body = b"lifespan=" + ",".join(_lifespan_log).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def lifespan_startup_failed(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.failed", "message": "database unavailable"})
        return
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def lifespan_shutdown_failed(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "lifespan":
        return
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.failed", "message": "cleanup failed"})
            return


async def lifespan_hangs(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        await receive()
        await asyncio.Event().wait()
    del send


async def lifespan_state(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                scope["state"]["ready"] = "yes"
                scope["state"]["shared"] = []
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    elif scope["type"] == "http":
        await receive()
        state = scope["state"]
        body = f"{state['ready']}:{state.get('request', 'missing')}:{len(state['shared'])}".encode()
        state["request"] = "local"
        state["shared"].append(1)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
    else:
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": scope["state"]["ready"]})
        await send({"type": "websocket.close", "code": 1000})
