"""WSGI app fixtures for tests/test_wsgi.py (not collected by ``unittest``).

The exported apps are wrapped in ``wsgiref.validate.validator`` so that any
PEP 3333 violation — by servery's engine *or* the app — raises during a request.
"""

from __future__ import annotations

from typing import Any
from wsgiref.validate import validator


def _echo(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    body = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
    payload = b"%s %s %s" % (
        environ["REQUEST_METHOD"].encode(),
        environ["PATH_INFO"].encode(),
        body,
    )
    start_response(
        "200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(payload)))]
    )
    return [payload]


def _streaming(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    # No Content-Length -> exercises the engine's chunked transfer-encoding path.
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"chunk1", b"chunk2", b"chunk3"]


def plain_list(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    # Raw (un-wrapped) app returning a materialized list WITHOUT Content-Length;
    # exercises the engine's coalesced one-write + Content-Length fast path.
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"materialized ", environ["REQUEST_METHOD"].encode()]


# Validator-wrapped: each request checks compliance on both sides. (The wrapper
# returns a non-list iterable, so these exercise the streaming/chunked path.)
application = validator(_echo)
streaming = validator(_streaming)
