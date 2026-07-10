"""ASGI app fixtures for tests/test_asgi.py (not collected by ``unittest``)."""

from __future__ import annotations

from typing import Any

_lifespan_log: list[str] = []


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
