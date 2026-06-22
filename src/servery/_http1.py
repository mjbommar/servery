"""Shared HTTP/1.1 response primitives.

The synchronous response writers (``wsgi``, ``cgi``, ``_proxy``) and the chunked
writer all built status lines, header blocks, framing decisions, and the chunk
wire-format independently. These primitives are the single source of truth so the
implementations cannot drift (and bugs are fixed once).
"""

from __future__ import annotations

import enum

# A ready-to-send 500 used when an app errors before committing a response.
INTERNAL_ERROR: bytes = (
    b"HTTP/1.1 500 Internal Server Error\r\n"
    b"Content-Type: text/plain\r\nContent-Length: 21\r\nConnection: close\r\n\r\n"
    b"Internal Server Error"
)

CHUNK_TERMINATOR: bytes = b"0\r\n\r\n"

# A ready-to-send 401 challenge (used by the async servers, which can't call the
# sync handler's _authorized()).
UNAUTHORIZED: bytes = (
    b"HTTP/1.1 401 Unauthorized\r\n"
    b'WWW-Authenticate: Basic realm="servery"\r\n'
    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
)


def chunk(data: bytes) -> bytes:
    """Encode one HTTP/1.1 chunked-transfer-encoding chunk."""
    return b"%x\r\n%s\r\n" % (len(data), data)


class Framing(enum.Enum):
    """How the caller must write/delimit the response body after :func:`build_head`."""

    CONTENT_LENGTH = "content-length"  # a Content-Length is present — write the body raw
    CHUNKED = "chunked"  # Transfer-Encoding: chunked added — wrap body writes in chunk()
    CLOSE = "close"  # body delimited by closing the connection — caller must close


def build_head(
    *,
    version: str,
    status: str,
    headers: list[tuple[str, str]],
    is_head: bool,
    keep_alive: bool,
    server: str | None = None,
    date: str | None = None,
    default_content_type: str | None = None,
    body_len: int | None = None,
) -> tuple[bytes, Framing]:
    """Build an encoded HTTP/1.1 response head and report how to frame the body.

    The single source of truth for the status line, header serialization, the
    Date/Server/Content-Type backfill, and the Content-Length vs chunked vs
    Connection: close decision — so the response writers can't drift.

    ``status`` is the full status-line tail (``"200 OK"``). ``keep_alive`` is
    whether this connection may be reused (HTTP/1.1 and nothing forced a close).
    ``body_len`` supplies a Content-Length when the body is already materialized.
    """
    present = {name.lower() for name, _ in headers}
    lines = [f"{version} {status}"]
    lines += [f"{name}: {value}" for name, value in headers]
    if server is not None and "server" not in present:
        lines.append(f"Server: {server}")
    if date is not None and "date" not in present:
        lines.append(f"Date: {date}")
    if default_content_type is not None and not present & {"content-type", "location"}:
        lines.append(f"Content-Type: {default_content_type}")

    if "content-length" in present:
        framing = Framing.CONTENT_LENGTH
    elif body_len is not None:
        lines.append(f"Content-Length: {body_len}")
        framing = Framing.CONTENT_LENGTH
    elif keep_alive and not is_head:
        lines.append("Transfer-Encoding: chunked")
        framing = Framing.CHUNKED
    else:
        framing = Framing.CLOSE

    if framing is Framing.CLOSE or not keep_alive:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"), framing
