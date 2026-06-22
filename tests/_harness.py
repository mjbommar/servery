"""Shared test harness: run servery on an ephemeral port and talk to it.

Not collected by ``unittest discover`` (no ``test_`` prefix).
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator

from servery import _log
from servery.config import Config
from servery.server import make_server


class LogCapture(logging.Handler):
    """Collect servery log records for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


@contextlib.contextmanager
def capturing_logs(level: int = logging.DEBUG) -> Iterator[LogCapture]:
    """Attach a capturing handler to servery's logger for the duration."""
    cap = LogCapture()
    previous = _log.logger.level
    _log.logger.addHandler(cap)
    _log.logger.setLevel(level)
    try:
        yield cap
    finally:
        _log.logger.removeHandler(cap)
        _log.logger.setLevel(previous)


def wait_for(predicate: Callable[[], object], timeout: float = 2.0) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses (for cross-thread logs)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@contextlib.contextmanager
def serving(config: Config) -> Iterator[tuple[str, int]]:
    """Run a server for ``config`` in a background thread; yield (host, port)."""
    httpd = make_server(config)
    host = str(httpd.server_address[0])
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def raw_exchange(host: str, port: int, request: bytes, timeout: float = 5.0) -> bytes:
    """Send raw bytes; return the full raw response (read until the peer closes).

    Use ``Connection: close`` in the request so the read terminates cleanly.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(request)
        sock.settimeout(timeout)
        chunks: list[bytes] = []
        while True:
            try:
                data = sock.recv(65536)
            except (TimeoutError, OSError):
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        sock.close()


def get_raw(host: str, port: int, target: str, extra: str = "") -> bytes:
    """Send a raw HTTP/1.1 GET for an arbitrary (unnormalized) target."""
    request = f"GET {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n{extra}\r\n"
    return raw_exchange(host, port, request.encode("latin-1"))


def status_of(response: bytes) -> int:
    """Parse the status code from a raw HTTP response."""
    line = response.split(b"\r\n", 1)[0]
    return int(line.split(b" ")[1])


def body_of(response: bytes) -> bytes:
    """Return the body (everything after the header/body separator)."""
    _, _, body = response.partition(b"\r\n\r\n")
    return body
