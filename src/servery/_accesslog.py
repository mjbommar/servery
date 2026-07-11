"""Access logging to a file — Common Log Format, Combined, or JSON lines.

Separate from the diagnostic stderr logger (``servery._log``): this writes one
structured line per response to ``--access-log`` for ops/analytics. Uses a
``logging.FileHandler`` (thread-safe, line-buffered) so it's safe under the
threading server. Covers the HTTP/1.1 file-serving surface (file/listing/error/
redirect/OPTIONS/upload/WebDAV responses) — everything that flows through the
handler's ``end_headers``.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

_FORMATS = ("clf", "combined", "json")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class AccessLogResult(StrEnum):
    """Producer-visible result without leaking queue implementation details."""

    ACCEPTED = "accepted"
    DROPPED_CAPACITY = "dropped_capacity"
    DROPPED_BYTES = "dropped_bytes"
    SINK_FAILED = "sink_failed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class AccessLogSnapshot:
    """Bounded-cardinality queue, delivery, and failure accounting."""

    accepted: int
    written: int
    write_failed: int
    abandoned: int
    dropped_capacity: int
    dropped_bytes: int
    rejected_sink_failed: int
    rejected_closed: int
    queued: int
    active: int
    outstanding_bytes: int
    high_water: int
    byte_high_water: int
    sink_failed: bool
    closed: bool
    writer_alive: bool


@dataclass(frozen=True, slots=True)
class _AccessRecord:
    client: str
    requestline: str
    status: int | str
    size: int | str
    referer: str
    user_agent: str
    when: float


def _record_charge(record: _AccessRecord) -> int:
    """Conservatively charge retained strings plus record/container overhead."""
    text = (
        record.client,
        record.requestline,
        str(record.status),
        str(record.size),
        record.referer,
        record.user_agent,
    )
    return 256 + sum(len(value.encode("utf-8", "replace")) for value in text)


def _escape_text(value: str) -> str:
    """Keep CLF/combined fields on one parseable line."""
    escaped: list[str] = []
    for char in value:
        code = ord(char)
        if char in {'"', "\\"}:
            escaped.append("\\" + char)
        elif code < 0x20 or code == 0x7F:
            escaped.append(f"\\x{code:02x}")
        else:
            escaped.append(char)
    return "".join(escaped)


class _FileHandler(logging.FileHandler):
    """File handler with transport-selectable write-error propagation."""

    def __init__(self, path: str, *, raise_errors: bool) -> None:
        super().__init__(path, encoding="utf-8")
        self._raise_errors = raise_errors

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        if self._raise_errors:
            error = sys.exception()
            if error is not None:
                raise error
        super().handleError(record)


def _clf_time(when: float) -> str:
    tm = time.localtime(when)
    offset = -(time.altzone if tm.tm_isdst else time.timezone)
    sign = "+" if offset >= 0 else "-"
    hh, mm = divmod(abs(offset) // 60, 60)
    return (
        f"{tm.tm_mday:02d}/{_MONTHS[tm.tm_mon - 1]}/{tm.tm_year:04d}:"
        f"{tm.tm_hour:02d}:{tm.tm_min:02d}:{tm.tm_sec:02d} {sign}{hh:02d}{mm:02d}"
    )


class AccessLog:
    """Append-only access log in ``clf`` / ``combined`` / ``json`` format."""

    def __init__(self, path: str, fmt: str = "clf", *, raise_errors: bool = False) -> None:
        if fmt not in _FORMATS:
            raise ValueError(f"access-log format must be one of {_FORMATS}, got {fmt!r}")
        self._fmt = fmt
        self._handler: logging.FileHandler | None = _FileHandler(path, raise_errors=raise_errors)
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._lock = threading.Lock()

    def close(self) -> None:
        """Detach and close the file handler, releasing the OS file handle.

        Called on server shutdown; without it the open handle would leak (and on
        Windows would block deleting the directory the log lives in).
        """
        with self._lock:
            handler, self._handler = self._handler, None
            if handler is not None:
                handler.close()

    def record(
        self,
        client: str,
        requestline: str,
        status: int | str,
        size: int | str,
        *,
        referer: str = "-",
        user_agent: str = "-",
        when: float | None = None,
    ) -> None:
        """Format and write one response record."""
        self.write_lines(
            (
                self.format_line(
                    client,
                    requestline,
                    status,
                    size,
                    referer=referer,
                    user_agent=user_agent,
                    when=when,
                ),
            )
        )

    def format_line(
        self,
        client: str,
        requestline: str,
        status: int | str,
        size: int | str,
        *,
        referer: str = "-",
        user_agent: str = "-",
        when: float | None = None,
    ) -> str:
        """Format one line without file I/O so transports may batch writes."""
        when = time.time() if when is None else when
        if self._fmt == "json":
            method, _, rest = requestline.partition(" ")
            path, _, proto = rest.rpartition(" ")
            line = json.dumps(
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + "Z",
                    "client": client,
                    "method": method,
                    "path": path,
                    "protocol": proto,
                    "status": status,
                    "size": size,
                    "referer": referer,
                    "user_agent": user_agent,
                }
            )
        else:
            client = _escape_text(client)
            requestline = _escape_text(requestline)
            line = f'{client} - - [{_clf_time(when)}] "{requestline}" {status} {size}'
            if self._fmt == "combined":
                line += f' "{_escape_text(referer)}" "{_escape_text(user_agent)}"'
        return line

    def write_lines(self, lines: Sequence[str]) -> None:
        """Write one or more preformatted lines with one locked handler flush."""
        if not lines:
            return
        record = logging.LogRecord(
            "servery.access",
            logging.INFO,
            "",
            0,
            "\n".join(lines),
            (),
            None,
        )
        with self._lock:
            if self._handler is not None:
                self._handler.emit(record)


class AsyncAccessLog:
    """One bounded, batched writer with explicit overload and drain policy."""

    def __init__(
        self,
        path: str,
        fmt: str = "clf",
        *,
        queue_capacity: int = 256,
        queue_byte_capacity: int = 8 * 1024 * 1024,
        overflow: str = "block",
        batch_size: int = 8,
        batch_wait: float = 0.001,
    ) -> None:
        if queue_capacity < 0:
            raise ValueError("access-log queue capacity cannot be negative")
        if queue_byte_capacity <= 0:
            raise ValueError("access-log queue byte capacity must be positive")
        if overflow not in {"block", "drop"}:
            raise ValueError("access-log overflow must be block or drop")
        if batch_size <= 0:
            raise ValueError("access-log batch size must be positive")
        if batch_wait < 0:
            raise ValueError("access-log batch wait cannot be negative")
        self._sink = AccessLog(path, fmt, raise_errors=True)
        self.queue_capacity = queue_capacity
        self.queue_byte_capacity = queue_byte_capacity
        self.overflow = overflow
        self.batch_size = batch_size
        self.batch_wait = batch_wait
        # Capacity includes one active record plus the configured waiting queue.
        self._count_capacity = queue_capacity + 1
        self._condition = threading.Condition()
        self._records: deque[tuple[_AccessRecord, int]] = deque()
        self._accepting = True
        self._closing = False
        self._closed = False
        self._sink_failed = False
        self._active = 0
        self._outstanding = 0
        self._outstanding_bytes = 0
        self._accepted = 0
        self._written = 0
        self._write_failed = 0
        self._abandoned = 0
        self._dropped_capacity = 0
        self._dropped_bytes = 0
        self._rejected_sink_failed = 0
        self._rejected_closed = 0
        self._high_water = 0
        self._byte_high_water = 0
        self._thread = threading.Thread(
            target=self._run,
            name="servery-access-log",
            daemon=True,
        )
        self._thread.start()

    def record(
        self,
        client: str,
        requestline: str,
        status: int | str,
        size: int | str,
        *,
        referer: str = "-",
        user_agent: str = "-",
        when: float | None = None,
    ) -> AccessLogResult:
        """Accept one immutable record, backpressure, or report an explicit drop."""
        record = _AccessRecord(
            client,
            requestline,
            status,
            size,
            referer,
            user_agent,
            time.time() if when is None else when,
        )
        charge = _record_charge(record)
        with self._condition:
            while True:
                if not self._accepting:
                    self._rejected_closed += 1
                    return AccessLogResult.CLOSED
                if self._sink_failed:
                    self._rejected_sink_failed += 1
                    return AccessLogResult.SINK_FAILED
                count_full = self._outstanding >= self._count_capacity
                bytes_full = self._outstanding_bytes + charge > self.queue_byte_capacity
                if not count_full and not bytes_full:
                    break
                # A record larger than the whole byte budget can never be admitted.
                if bytes_full and (charge > self.queue_byte_capacity or self.overflow == "drop"):
                    self._dropped_bytes += 1
                    return AccessLogResult.DROPPED_BYTES
                if count_full and self.overflow == "drop":
                    self._dropped_capacity += 1
                    return AccessLogResult.DROPPED_CAPACITY
                self._condition.wait()
            self._records.append((record, charge))
            self._outstanding += 1
            self._outstanding_bytes += charge
            self._accepted += 1
            self._high_water = max(self._high_water, self._outstanding)
            self._byte_high_water = max(self._byte_high_water, self._outstanding_bytes)
            self._condition.notify_all()
            return AccessLogResult.ACCEPTED

    def _next_batch(self) -> list[tuple[_AccessRecord, int]] | None:
        with self._condition:
            while not self._records and not self._closing:
                self._condition.wait()
            if not self._records:
                return None
            batch = [self._records.popleft()]
            deadline = time.monotonic() + self.batch_wait
            while len(batch) < self.batch_size:
                while not self._records and not self._closing:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                if not self._records:
                    break
                batch.append(self._records.popleft())
            self._active = len(batch)
            return batch

    def _run(self) -> None:
        try:
            while True:
                batch = self._next_batch()
                if batch is None:
                    return
                failed = False
                try:
                    self._sink.write_lines(
                        [
                            self._sink.format_line(
                                item.client,
                                item.requestline,
                                item.status,
                                item.size,
                                referer=item.referer,
                                user_agent=item.user_agent,
                                when=item.when,
                            )
                            for item, _charge in batch
                        ]
                    )
                except Exception:
                    failed = True
                    logging.getLogger("servery").error(
                        "access-log sink failed; rejecting later records",
                        exc_info=True,
                    )
                released_bytes = sum(charge for _record, charge in batch)
                with self._condition:
                    self._active = 0
                    self._outstanding -= len(batch)
                    self._outstanding_bytes -= released_bytes
                    if failed:
                        self._write_failed += len(batch)
                        self._sink_failed = True
                        self._abandoned += len(self._records)
                        self._outstanding -= len(self._records)
                        self._outstanding_bytes -= sum(charge for _record, charge in self._records)
                        self._records.clear()
                    else:
                        self._written += len(batch)
                    self._condition.notify_all()
        finally:
            self._sink.close()
            with self._condition:
                self._closed = True
                self._condition.notify_all()

    def close(self, *, timeout: float | None = None) -> AccessLogSnapshot:
        """Stop admission and drain accepted records for at most ``timeout``."""
        if timeout is not None and timeout < 0:
            raise ValueError("access-log close timeout cannot be negative")
        with self._condition:
            self._accepting = False
            self._closing = True
            self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        return self.snapshot()

    def snapshot(self) -> AccessLogSnapshot:
        """Return a thread-safe accounting snapshot."""
        with self._condition:
            return AccessLogSnapshot(
                accepted=self._accepted,
                written=self._written,
                write_failed=self._write_failed,
                abandoned=self._abandoned,
                dropped_capacity=self._dropped_capacity,
                dropped_bytes=self._dropped_bytes,
                rejected_sink_failed=self._rejected_sink_failed,
                rejected_closed=self._rejected_closed,
                queued=len(self._records),
                active=self._active,
                outstanding_bytes=self._outstanding_bytes,
                high_water=self._high_water,
                byte_high_water=self._byte_high_water,
                sink_failed=self._sink_failed,
                closed=self._closed,
                writer_alive=self._thread.is_alive(),
            )
