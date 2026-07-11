"""Shared response-write progress deadlines for sync and asyncio transports."""

from __future__ import annotations

import asyncio
import contextlib
import io
import socket
from collections.abc import Buffer, Generator
from typing import Any, BinaryIO, cast


@contextlib.contextmanager
def socket_timeout(sock: socket.socket, timeout: float) -> Generator[None]:
    """Apply ``timeout`` to one blocking socket write, then restore the prior value."""
    previous = sock.gettimeout()
    if previous == timeout:
        yield
        return
    sock.settimeout(timeout)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            sock.settimeout(previous)


class DeadlineWriter(io.BufferedIOBase):
    """File-like writer that applies a socket timeout to each write/flush operation."""

    __slots__ = ("_raw", "_sock", "_timeout")

    def __init__(self, raw: BinaryIO, sock: socket.socket, timeout: float) -> None:
        self._raw = raw
        self._sock = sock
        self._timeout = timeout

    @property
    def closed(self) -> bool:
        return self._raw.closed

    def write(self, data: Buffer) -> int:
        with socket_timeout(self._sock, self._timeout):
            return self._raw.write(cast("bytes", data))

    def writable(self) -> bool:
        return True

    def flush(self) -> None:
        with socket_timeout(self._sock, self._timeout):
            self._raw.flush()

    def close(self) -> None:
        self._raw.close()

    def fileno(self) -> int:
        return self._raw.fileno()


async def drain(writer: Any, timeout: float | None) -> None:
    """Drain an asyncio writer, optionally bounding a lack of write progress."""
    if timeout is None:
        await writer.drain()
        return
    transport = getattr(writer, "transport", None)
    if transport is not None:
        low_water, _high_water = transport.get_write_buffer_limits()
        if transport.get_write_buffer_size() <= low_water:
            # At or below the transport's resume threshold, drain cannot remain
            # paused for write-buffer relief. Preserve its error/closing checks
            # without allocating and scheduling a deadline for this common path.
            await writer.drain()
            return
    async with asyncio.timeout(timeout):
        await writer.drain()
