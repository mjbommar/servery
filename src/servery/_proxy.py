"""Reverse-proxy forwarding (opt-in via ``--proxy PREFIX=UPSTREAM``).

A matching request is forwarded to the upstream origin and its response streamed
back. Pure stdlib (``http.client``). Hop-by-hop headers are stripped both ways
(RFC 9110 §7.6.1) and ``X-Forwarded-For``/``-Proto``/``-Host`` are added so the
upstream sees the real client. This makes servery a simple edge in front of an
app server (e.g. front static files, proxy ``/api`` to a backend).
"""

from __future__ import annotations

import contextlib
import http.client
import ssl
import urllib.parse
from typing import TYPE_CHECKING

from servery import _log

if TYPE_CHECKING:
    from servery.handler import ServeryHandler

# Hop-by-hop headers must not be forwarded (RFC 9110 §7.6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def target_for(path: str, routes: tuple[tuple[str, str], ...]) -> str | None:
    """Return the upstream URL for ``path`` if a route prefix matches."""
    for prefix, upstream in routes:
        if path == prefix or prefix == "/" or path.startswith(prefix.rstrip("/") + "/"):
            return upstream.rstrip("/") + path  # forward the full path
    return None


def forward(handler: ServeryHandler, target: str) -> None:
    """Forward the current request to ``target`` and stream the response back."""
    config = handler._server.config
    parsed = urllib.parse.urlsplit(target)
    length = int(handler.headers.get("content-length") or 0)
    if length > config.max_upload_size:
        handler.send_error(413, "Request body too large to proxy")
        return
    body = handler.rfile.read(length) if length else None

    scheme = "https" if isinstance(handler.connection, ssl.SSLSocket) else "http"
    out_headers = {
        name: value
        for name, value in handler.headers.items()
        if name.lower() not in _HOP_BY_HOP and name.lower() != "host"
    }
    out_headers["Host"] = parsed.netloc
    out_headers["X-Forwarded-For"] = handler.client_address[0]
    out_headers["X-Forwarded-Proto"] = scheme
    out_headers.setdefault("X-Forwarded-Host", handler.headers.get("host", parsed.netloc))

    connector = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    default_port = 443 if parsed.scheme == "https" else 80
    conn = connector(parsed.hostname or "", parsed.port or default_port, timeout=config.timeout)
    upstream_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        conn.request(handler.command or "GET", upstream_path, body=body, headers=out_headers)
        response = conn.getresponse()
        relay_headers = [(k, v) for k, v in response.getheaders() if k.lower() not in _HOP_BY_HOP]
        _relay(handler, response.status, response.reason, relay_headers, response)
    except (OSError, http.client.HTTPException) as exc:
        _log.logger.warning("proxy to %s failed: %r", target, exc)
        with contextlib.suppress(OSError):
            handler.send_error(502, "Bad gateway")
    finally:
        conn.close()


def _relay(
    handler: ServeryHandler,
    status: int,
    reason: str,
    headers: list[tuple[str, str]],
    response: http.client.HTTPResponse,
) -> None:
    present = {name.lower() for name, _ in headers}
    lines = [f"{handler.protocol_version} {status} {reason}"]
    lines += [f"{name}: {value}" for name, value in headers]
    # Upstream gave no Content-Length (e.g. it was chunked, which we stripped) ->
    # delimit the body by closing the connection.
    if "content-length" not in present:
        handler.close_connection = True
        lines.append("Connection: close")
    handler.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    if handler.command != "HEAD":
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            handler.wfile.write(chunk)
    handler.log_request(status)
