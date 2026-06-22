"""Shared HTTP/1.1 response primitives.

The synchronous response writers (``wsgi``, ``cgi``, ``_proxy``) and the chunked
writer all built status lines, header blocks, framing decisions, and the chunk
wire-format independently. These primitives are the single source of truth so the
implementations cannot drift (and bugs are fixed once).
"""

from __future__ import annotations

# A ready-to-send 500 used when an app errors before committing a response.
INTERNAL_ERROR: bytes = (
    b"HTTP/1.1 500 Internal Server Error\r\n"
    b"Content-Type: text/plain\r\nContent-Length: 21\r\nConnection: close\r\n\r\n"
    b"Internal Server Error"
)

CHUNK_TERMINATOR: bytes = b"0\r\n\r\n"


def chunk(data: bytes) -> bytes:
    """Encode one HTTP/1.1 chunked-transfer-encoding chunk."""
    return b"%x\r\n%s\r\n" % (len(data), data)
