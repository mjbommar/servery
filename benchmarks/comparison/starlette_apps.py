"""Pinned Starlette applications used only by the comparison harness."""

from __future__ import annotations

from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

_STREAM_CHUNK = (b"framework-stream\n" * 256)[:4096]
_STREAM_COUNT = 16


async def _json(request: Request) -> JSONResponse:
    """Exercise Starlette's request URL/query wrappers and JSON response."""
    return JSONResponse(
        {
            "framework": "starlette",
            "path": request.url.path,
            "q": request.query_params.get("q", ""),
        }
    )


async def _stream_chunks() -> AsyncIterator[bytes]:
    for _ in range(_STREAM_COUNT):
        yield _STREAM_CHUNK


async def _stream(_request: Request) -> StreamingResponse:
    """Exercise StreamingResponse's producer and disconnect-listener task group."""
    length = len(_STREAM_CHUNK) * _STREAM_COUNT
    return StreamingResponse(
        _stream_chunks(),
        media_type="application/octet-stream",
        headers={"content-length": str(length)},
    )


app = Starlette(
    routes=[
        Route("/starlette/json", _json),
        Route("/starlette/stream", _stream),
    ]
)
