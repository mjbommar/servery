"""Bare WSGI/ASGI apps for benchmarking servery's OWN overhead.

Deliberately minimal — no ``wsgiref.validate`` wrapper (that's what tests use for
compliance) — so the numbers reflect servery's request/response machinery, not the
validator or app logic.
"""

from __future__ import annotations

from typing import Any

_BODY = b"ok"


def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    """A trivial PEP 3333 app: fixed 2-byte text body, Content-Length set."""
    start_response(
        "200 OK",
        [("Content-Type", "text/plain"), ("Content-Length", str(len(_BODY)))],
    )
    return [_BODY]


async def asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """A trivial ASGI 3.0 HTTP app mirroring :func:`wsgi_app`."""
    assert scope["type"] == "http"
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": _BODY})
