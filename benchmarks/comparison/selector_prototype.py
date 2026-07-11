"""Production-shaped, benchmark-only selector HTTP/1 static frontend.

This remains deliberately outside servery's public runtime. It tests whether the
selector ceiling survives realistic connection state, admission, deadlines,
partial/pipelined input ownership, cancellation, and graceful drain before the
project commits to a second production backend. Static GET/HEAD is the only
feature cohort; unsupported request bodies close the connection after one reply.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import mimetypes
import os
import queue
import stat as stat_module
import threading
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, BinaryIO

from servery import (
    _accesslog,
    _compress,
    _digest,
    _http1,
    _request,
    _static,
    _write,
    listing,
    security,
)

_READ_SIZE = 64 * 1024
_SMALL_BODY = 16 * 1024
_SENDFILE_PROGRESS = 1024 * 1024


@dataclass(slots=True)
class Stats:
    """Bounded-cardinality lifecycle counters for prototype acceptance tests."""

    accepted: int = 0
    rejected: int = 0
    completed: int = 0
    parse_errors: int = 0
    head_timeouts: int = 0
    write_timeouts: int = 0
    cancelled: int = 0
    filesystem_submitted: int = 0
    filesystem_rejected: int = 0
    filesystem_cancelled: int = 0
    filesystem_late_closed: int = 0
    listing_submitted: int = 0
    listing_rejected: int = 0
    listing_cancelled: int = 0
    listing_late_completed: int = 0
    listing_shutdown_cancelled: int = 0
    listing_errors: int = 0
    compression_hits: int = 0
    compression_submitted: int = 0
    compression_shared: int = 0
    compression_rejected: int = 0
    compression_cancelled: int = 0
    compression_late_completed: int = 0
    compression_errors: int = 0
    digest_hits: int = 0
    digest_submitted: int = 0
    digest_shared: int = 0
    digest_rejected: int = 0
    digest_cancelled: int = 0
    digest_late_completed: int = 0
    digest_errors: int = 0
    access_log_submitted: int = 0
    access_log_dropped: int = 0
    access_log_errors: int = 0
    transfer_errors: int = 0


@dataclass(frozen=True, slots=True)
class Policy:
    active_timeout: float = 30.0
    keepalive_timeout: float | None = None
    write_timeout: float | None = None
    max_connections: int | None = 256
    max_requests: int = 0
    drain_timeout: float = 5.0
    small_file_buffer_size: int = _SMALL_BODY
    filesystem_workers: int = 0
    filesystem_queue: int = 0
    filesystem_delay: float = 0.0
    listing_workers: int = 0
    listing_queue: int = 0
    listing_delay: float = 0.0
    show_hidden: bool = False
    max_listing_entries: int = 100_000
    listing_page_size: int = 1000
    listing_details_threshold: int = 10_000
    cache_control: str = "no-cache"
    spa: bool = False
    compress: bool = False
    max_compress_size: int = _compress.GZIP_MAX
    compression_cache_size: int = 0
    compression_workers: int = 0
    compression_queue: int = 0
    compression_delay: float = 0.0
    digest_cache_size: int = 0
    digest_workers: int = 0
    digest_queue: int = 0
    digest_delay: float = 0.0
    access_log: str | None = None
    access_log_format: str = "clf"
    access_log_queue: int = 256
    access_log_overflow: str = "drop"
    access_log_batch_size: int = 8
    access_log_batch_wait: float = 0.001
    access_log_delay: float = 0.0

    def __post_init__(self) -> None:
        if self.active_timeout <= 0:
            raise ValueError("active_timeout must be positive")
        if self.keepalive_timeout is not None and self.keepalive_timeout <= 0:
            raise ValueError("keepalive_timeout must be positive")
        if self.write_timeout is not None and self.write_timeout <= 0:
            raise ValueError("write_timeout must be positive")
        if self.max_connections is not None and self.max_connections <= 0:
            raise ValueError("max_connections must be positive")
        if self.max_requests < 0:
            raise ValueError("max_requests cannot be negative")
        if self.drain_timeout < 0:
            raise ValueError("drain_timeout cannot be negative")
        if self.small_file_buffer_size < 0:
            raise ValueError("small_file_buffer_size cannot be negative")
        if self.filesystem_workers < 0:
            raise ValueError("filesystem_workers cannot be negative")
        if self.filesystem_queue < 0:
            raise ValueError("filesystem_queue cannot be negative")
        if self.filesystem_workers == 0 and self.filesystem_queue:
            raise ValueError("filesystem_queue requires filesystem_workers")
        if self.filesystem_delay < 0:
            raise ValueError("filesystem_delay cannot be negative")
        if self.listing_workers < 0:
            raise ValueError("listing_workers cannot be negative")
        if self.listing_queue < 0:
            raise ValueError("listing_queue cannot be negative")
        if self.listing_workers == 0 and self.listing_queue:
            raise ValueError("listing_queue requires listing_workers")
        if self.listing_delay < 0:
            raise ValueError("listing_delay cannot be negative")
        if self.max_listing_entries <= 0:
            raise ValueError("max_listing_entries must be positive")
        if self.listing_page_size <= 0:
            raise ValueError("listing_page_size must be positive")
        if self.listing_details_threshold <= 0:
            raise ValueError("listing_details_threshold must be positive")
        if self.max_compress_size < 0:
            raise ValueError("max_compress_size cannot be negative")
        if self.compression_cache_size < 0:
            raise ValueError("compression_cache_size cannot be negative")
        if self.compression_workers < 0:
            raise ValueError("compression_workers cannot be negative")
        if self.compression_queue < 0:
            raise ValueError("compression_queue cannot be negative")
        if self.compression_workers == 0 and self.compression_queue:
            raise ValueError("compression_queue requires compression_workers")
        if self.compress and self.compression_workers == 0:
            raise ValueError("compress requires compression_workers")
        if self.compression_delay < 0:
            raise ValueError("compression_delay cannot be negative")
        if self.digest_cache_size < 0:
            raise ValueError("digest_cache_size cannot be negative")
        if self.digest_workers < 0:
            raise ValueError("digest_workers cannot be negative")
        if self.digest_queue < 0:
            raise ValueError("digest_queue cannot be negative")
        if self.digest_workers == 0 and self.digest_queue:
            raise ValueError("digest_queue requires digest_workers")
        if self.digest_delay < 0:
            raise ValueError("digest_delay cannot be negative")
        if self.access_log_format not in {"clf", "combined", "json"}:
            raise ValueError("access_log_format must be clf, combined, or json")
        if self.access_log_queue < 0:
            raise ValueError("access_log_queue cannot be negative")
        if self.access_log_overflow not in {"drop", "wait"}:
            raise ValueError("access_log_overflow must be drop or wait")
        if self.access_log_batch_size <= 0:
            raise ValueError("access_log_batch_size must be positive")
        if self.access_log_batch_wait < 0:
            raise ValueError("access_log_batch_wait cannot be negative")
        if self.access_log_delay < 0:
            raise ValueError("access_log_delay cannot be negative")
        if "\r" in self.cache_control or "\n" in self.cache_control:
            raise ValueError("cache_control cannot contain CR or LF")
        try:
            self.cache_control.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError("cache_control must be HTTP/1-compatible text") from exc


class _FilesystemBusyError(Exception):
    """The bounded filesystem executor has no worker or queue capacity."""


class _CompressionBusyError(Exception):
    """The bounded compression executor has no worker or queue capacity."""


class _ListingBusyError(Exception):
    """The bounded directory-listing executor has no worker or queue capacity."""


class _ListingReadError(Exception):
    """The selected directory could not be scanned."""


class _ListingFailureError(Exception):
    """A listing worker failed outside an expected filesystem error."""


class _CompressionFailureError(Exception):
    """A compression worker could not produce the selected representation."""


class _DigestBusyError(Exception):
    """The bounded digest executor has no worker or queue capacity."""


class _DigestFailureError(Exception):
    """A digest worker could not hash the selected representation."""


@dataclass(frozen=True, slots=True)
class _AccessRecord:
    client: str
    requestline: str
    status: int
    size: int | str
    referer: str
    user_agent: str
    when: float


def _write_access_batch(
    log: _accesslog.AccessLog,
    records: Sequence[_AccessRecord],
    delay: float,
) -> None:
    """Format immutable records and flush them together on the logging worker."""
    lines: list[str] = []
    for record in records:
        if delay:
            time.sleep(delay)
        lines.append(
            log.format_line(
                record.client,
                record.requestline,
                record.status,
                record.size,
                referer=record.referer,
                user_agent=record.user_agent,
                when=record.when,
            )
        )
    log.write_lines(lines)


_ACCESS_STOP = None


class _AccessPlanner:
    """Bounded, event-loop-safe access logging with explicit overflow policy."""

    def __init__(self, policy: Policy, stats: Stats) -> None:
        self.policy = policy
        self.stats = stats
        self.log = (
            _accesslog.AccessLog(policy.access_log, policy.access_log_format, raise_errors=True)
            if policy.access_log is not None
            else None
        )
        # Capacity includes the active write plus the explicitly configured backlog.
        total_capacity = policy.access_log_queue + 1
        self.wait_capacity = (
            asyncio.Semaphore(total_capacity)
            if self.log is not None and policy.access_log_overflow == "wait"
            else None
        )
        self.drop_capacity = (
            threading.BoundedSemaphore(total_capacity)
            if self.log is not None and policy.access_log_overflow == "drop"
            else None
        )
        self.loop = asyncio.get_running_loop()
        # The semaphore gates every insertion, so this queue is logically bounded even
        # though its storage primitive does not duplicate the capacity accounting.
        self.records: queue.Queue[_AccessRecord | None] = queue.Queue()
        self.worker_errors = 0
        self.thread = (
            threading.Thread(
                target=self._run,
                name="servery-selector-access",
                daemon=False,
            )
            if self.log is not None
            else None
        )
        if self.thread is not None:
            self.thread.start()

    def _run(self) -> None:
        log = self.log
        if log is None:  # pragma: no cover - constructor invariant
            return
        while True:
            first = self.records.get()
            if first is _ACCESS_STOP:
                self.records.task_done()
                return
            batch = [first]
            batch_deadline = time.monotonic() + self.policy.access_log_batch_wait
            while len(batch) < self.policy.access_log_batch_size:
                try:
                    if self.policy.access_log_batch_wait:
                        remaining = batch_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        record = self.records.get(timeout=remaining)
                    else:
                        record = self.records.get_nowait()
                except queue.Empty:
                    break
                if record is _ACCESS_STOP:  # pragma: no cover - close waits for an empty queue
                    self.records.task_done()
                    return
                batch.append(record)
            errors = 0
            try:
                _write_access_batch(log, batch, self.policy.access_log_delay)
            except Exception:
                errors = len(batch)
            finally:
                for _record in batch:
                    self.records.task_done()
            self.worker_errors += errors
            drop_capacity = self.drop_capacity
            if drop_capacity is not None:
                for _ in batch:
                    drop_capacity.release()
            else:
                self.loop.call_soon_threadsafe(self._release_wait_capacity, len(batch))

    def _release_wait_capacity(self, completed: int) -> None:
        capacity = self.wait_capacity
        if capacity is None:  # pragma: no cover - policy invariant
            return
        for _ in range(completed):
            capacity.release()

    async def record(
        self,
        client: str,
        requestline: str,
        status: int,
        size: int | str,
        headers: _request.RequestHeaders | None,
    ) -> None:
        """Accept one record, waiting or dropping only when the bounded budget is full."""
        log = self.log
        if log is None:
            return
        if self.policy.access_log_overflow == "drop":
            capacity = self.drop_capacity
            if capacity is None:  # pragma: no cover - policy invariant
                raise RuntimeError("drop access-log capacity is unavailable")
            if not capacity.acquire(blocking=False):
                self.stats.access_log_dropped += 1
                return
        else:
            wait_capacity = self.wait_capacity
            if wait_capacity is None:  # pragma: no cover - policy invariant
                raise RuntimeError("wait access-log capacity is unavailable")
            await wait_capacity.acquire()
        record = _AccessRecord(
            client,
            requestline,
            status,
            size,
            headers.get("Referer", "-") if headers is not None else "-",
            headers.get("User-Agent", "-") if headers is not None else "-",
            time.time(),
        )
        self.records.put_nowait(record)
        self.stats.access_log_submitted += 1

    async def close(self) -> None:
        """Drain every accepted record before closing the file."""
        thread = self.thread
        if thread is not None:
            await asyncio.to_thread(self.records.join)
            self.records.put(_ACCESS_STOP)
            await asyncio.to_thread(thread.join)
            self.stats.access_log_errors += self.worker_errors
        if self.log is not None:
            self.log.close()


@dataclass(frozen=True, slots=True)
class _RequestAccess:
    planner: _AccessPlanner
    client: str
    requestline: str
    headers: _request.RequestHeaders | None

    async def record(
        self,
        status: int,
        response_headers: Sequence[tuple[str, str]],
    ) -> None:
        size: int | str = "-"
        for name, value in response_headers:
            if name.lower() == "content-length":
                size = value
                break
        await self.planner.record(
            self.client,
            self.requestline,
            status,
            size,
            self.headers,
        )


@dataclass(slots=True)
class _PreparedFile:
    path: str
    handle: BinaryIO
    stat: os.stat_result
    content_type: str
    body: bytes | None
    coding: str | None
    etag: str
    last_modified: str

    def close(self) -> None:
        self.handle.close()


@dataclass(frozen=True, slots=True)
class _PreparedDirectory:
    path: str
    redirect: str | None

    def close(self) -> None:
        """Match the prepared-resource ownership interface; directories own no handle."""


_PreparedTarget = _PreparedFile | _PreparedDirectory


def _require_regular(stat: os.stat_result, path: str) -> None:
    if not stat_module.S_ISREG(stat.st_mode):
        raise FileNotFoundError(path)


def _read_exact_file(source: BinaryIO, size: int) -> bytes:
    data = source.read(size)
    if len(data) != size:
        raise OSError("file changed while reading the planned body")
    return data


def _prepare_file(
    path: str,
    method: str,
    small_file_buffer_size: int,
    delay: float,
    accept_encoding: str = "",
    compression_enabled: bool = False,
    max_compress_size: int = 0,
    allow_compression: bool = True,
) -> _PreparedFile:
    if delay:
        time.sleep(delay)
    ctype = _compress.with_charset(mimetypes.guess_file_type(path)[0] or "application/octet-stream")
    opened = _static.open_file(
        path,
        ctype,
        accept_encoding,
        compression_enabled=compression_enabled,
        max_compress_size=max_compress_size,
        allow_compression=allow_compression,
    )
    try:
        stat = opened.stat
        _require_regular(stat, path)
        body = None
        if method == "GET" and 0 < stat.st_size <= small_file_buffer_size:
            body = _read_exact_file(opened.handle, stat.st_size)
        return _PreparedFile(
            path,
            opened.handle,
            stat,
            opened.ctype,
            body,
            opened.coding,
            opened.etag,
            opened.last_modified,
        )
    except BaseException:
        opened.close()
        raise


def _prepare_target(
    root_real: str,
    path: str,
    target: str,
    method: str,
    small_file_buffer_size: int,
    delay: float,
    spa: bool,
    accept_encoding: str,
    compression_enabled: bool,
    max_compress_size: int,
    allow_compression: bool,
) -> _PreparedTarget:
    """Resolve directory redirect/index semantics, then acquire one regular identity."""
    if delay:
        time.sleep(delay)
    if os.path.isdir(path):  # noqa: PTH112 - matches the OS-level planner contract
        redirect = _static.directory_redirect(target)
        if redirect is not None:
            return _PreparedDirectory(path, redirect)
        index = _static.find_contained_index(root_real, path, ("index.html", "index.htm"))
        if index is None:
            return _PreparedDirectory(path, None)
        path = index
    elif spa and not os.path.exists(path):  # noqa: PTH110 - OS-level planner contract
        index = _static.find_contained_index(root_real, root_real, ("index.html",))
        if index is not None:
            path = index
    return _prepare_file(
        path,
        method,
        small_file_buffer_size,
        0.0,
        accept_encoding,
        compression_enabled,
        max_compress_size,
        allow_compression,
    )


class _FilesystemPlanner:
    """Inline or cancellation-safe bounded-executor file preparation."""

    def __init__(self, policy: Policy, stats: Stats) -> None:
        self.policy = policy
        self.stats = stats
        self.executor = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=policy.filesystem_workers,
                thread_name_prefix="servery-selector-fs",
            )
            if policy.filesystem_workers
            else None
        )
        capacity = policy.filesystem_workers + policy.filesystem_queue
        self.capacity = asyncio.Semaphore(capacity) if capacity else None
        self.futures: set[asyncio.Future[_PreparedTarget]] = set()

    async def prepare(
        self,
        root_real: str,
        path: str,
        target: str,
        method: str,
        accept_encoding: str,
        allow_compression: bool,
    ) -> _PreparedTarget:
        if self.executor is None:
            return _prepare_target(
                root_real,
                path,
                target,
                method,
                self.policy.small_file_buffer_size,
                self.policy.filesystem_delay,
                self.policy.spa,
                accept_encoding,
                self.policy.compress,
                self.policy.max_compress_size,
                allow_compression,
            )
        capacity = self.capacity
        if capacity is None:  # pragma: no cover - constructor invariant
            raise RuntimeError("filesystem capacity is missing")
        if capacity.locked():
            self.stats.filesystem_rejected += 1
            raise _FilesystemBusyError
        await capacity.acquire()
        self.stats.filesystem_submitted += 1
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self.executor,
            _prepare_target,
            root_real,
            path,
            target,
            method,
            self.policy.small_file_buffer_size,
            self.policy.filesystem_delay,
            self.policy.spa,
            accept_encoding,
            self.policy.compress,
            self.policy.max_compress_size,
            allow_compression,
        )
        self.futures.add(future)

        def finished(done: asyncio.Future[_PreparedTarget]) -> None:
            self.futures.discard(done)
            capacity.release()

        future.add_done_callback(finished)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.stats.filesystem_cancelled += 1

            def close_late(done: asyncio.Future[_PreparedTarget]) -> None:
                if not done.cancelled() and done.exception() is None:
                    done.result().close()
                    self.stats.filesystem_late_closed += 1

            future.add_done_callback(close_late)
            raise

    async def close(self) -> None:
        if self.futures:
            await asyncio.gather(*tuple(self.futures), return_exceptions=True)
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)


@dataclass(frozen=True, slots=True)
class _PreparedListing:
    body: bytes
    encoding: str | None
    theme_cookie: str | None


def _render_listing(
    path: str,
    options: listing.RequestOptions,
    policy: Policy,
    encoding: str | None,
) -> _PreparedListing:
    """Worker-side bounded scan/render and optional generated-body compression."""
    if policy.listing_delay:
        time.sleep(policy.listing_delay)
    body = listing.render(
        path,
        options.display,
        show_hidden=policy.show_hidden,
        sort=options.sort,
        order=options.order,
        query=options.query,
        ext=options.ext,
        page=options.page,
        per_page=policy.listing_page_size,
        theme=options.theme,
        max_entries=policy.max_listing_entries,
        details_threshold=policy.listing_details_threshold,
    )
    if encoding is not None:
        body = _compress.encode(body, encoding)
    theme_cookie = (
        f"servery_theme={options.theme}; Path=/; Max-Age=31536000; SameSite=Lax"
        if options.set_theme_cookie
        else None
    )
    return _PreparedListing(body, encoding, theme_cookie)


class _ListingPlanner:
    """Optional cancellation-safe bounded executor for generated directory pages."""

    def __init__(self, policy: Policy, stats: Stats) -> None:
        self.policy = policy
        self.stats = stats
        self.executor = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=policy.listing_workers,
                thread_name_prefix="servery-selector-listing",
            )
            if policy.listing_workers
            else None
        )
        capacity = policy.listing_workers + policy.listing_queue
        self.capacity = asyncio.Semaphore(capacity) if capacity else None
        self.futures: set[asyncio.Future[_PreparedListing]] = set()
        self.jobs: dict[
            asyncio.Future[_PreparedListing], concurrent.futures.Future[_PreparedListing]
        ] = {}

    async def render(
        self,
        path: str,
        target: str,
        cookie: str | None,
        accept_encoding: str,
    ) -> _PreparedListing | None:
        """Return a generated page, or ``None`` when listing support is disabled."""
        executor = self.executor
        if executor is None:
            return None
        options = listing.request_options(target, cookie)
        encoding = _compress.negotiate(accept_encoding, enabled=self.policy.compress)
        capacity = self.capacity
        if capacity is None:  # pragma: no cover - policy invariant
            raise RuntimeError("listing capacity is unavailable")
        if capacity.locked():
            self.stats.listing_rejected += 1
            raise _ListingBusyError
        await capacity.acquire()
        try:
            job = executor.submit(
                _render_listing,
                path,
                options,
                self.policy,
                encoding,
            )
            future = asyncio.wrap_future(job)
        except BaseException:
            capacity.release()
            raise
        self.stats.listing_submitted += 1
        self.futures.add(future)
        self.jobs[future] = job

        def finished(done: asyncio.Future[_PreparedListing]) -> None:
            self.futures.discard(done)
            self.jobs.pop(done, None)
            capacity.release()

        future.add_done_callback(finished)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.stats.listing_cancelled += 1

            def completed_late(done: asyncio.Future[_PreparedListing]) -> None:
                if not done.cancelled():
                    self.stats.listing_late_completed += 1

            future.add_done_callback(completed_late)
            raise
        except OSError as exc:
            self.stats.listing_errors += 1
            raise _ListingReadError from exc
        except Exception as exc:
            self.stats.listing_errors += 1
            raise _ListingFailureError from exc

    async def close(self) -> None:
        for job in tuple(self.jobs.values()):
            if job.cancel():
                self.stats.listing_shutdown_cancelled += 1
        if self.futures:
            await asyncio.gather(*tuple(self.futures), return_exceptions=True)
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)


def _encode_prepared(
    cache: _compress.CompressionCache,
    key: _compress.CacheKey,
    coding: str,
    body: bytes | None,
    source_fd: int | None,
    expected_size: int,
    delay: float,
) -> bytes:
    """Worker-side cache lookup and encoding over bytes or an owned duplicate fd."""
    source = os.fdopen(source_fd, "rb") if source_fd is not None else None
    try:

        def produce() -> bytes:
            if delay:
                time.sleep(delay)
            if body is not None:
                data = body
            elif source is not None:
                source.seek(0)
                data = _read_exact_file(source, expected_size)
            else:  # pragma: no cover - caller invariant
                raise RuntimeError("compression source is missing")
            return _compress.encode(data, coding)

        return cache.get_or_compute(key, produce)
    finally:
        if source is not None:
            source.close()


class _CompressionPlanner:
    """Cache-first, single-flight, bounded-executor coded-body preparation."""

    def __init__(self, policy: Policy, stats: Stats) -> None:
        self.policy = policy
        self.stats = stats
        self.cache = _compress.CompressionCache(policy.compression_cache_size)
        self.executor = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=policy.compression_workers,
                thread_name_prefix="servery-selector-compress",
            )
            if policy.compress
            else None
        )
        capacity = policy.compression_workers + policy.compression_queue
        self.capacity = asyncio.Semaphore(capacity) if policy.compress else None
        self.futures: set[asyncio.Future[bytes]] = set()
        self.inflight: dict[_compress.CacheKey, asyncio.Future[bytes]] = {}

    async def _wait(self, future: asyncio.Future[bytes]) -> bytes:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.stats.compression_cancelled += 1

            def completed_late(_done: asyncio.Future[bytes]) -> None:
                self.stats.compression_late_completed += 1

            future.add_done_callback(completed_late)
            raise
        except Exception as exc:
            self.stats.compression_errors += 1
            raise _CompressionFailureError from exc

    async def encode(self, prepared: _PreparedFile) -> bytes:
        coding = prepared.coding
        if coding is None:  # pragma: no cover - caller invariant
            raise RuntimeError("compression requested for an identity representation")
        key = _compress.cache_key(prepared.path, prepared.stat, coding)
        cached = self.cache.get(key)
        if cached is not None:
            self.stats.compression_hits += 1
            return cached
        existing = self.inflight.get(key)
        if existing is not None:
            self.stats.compression_shared += 1
            return await self._wait(existing)

        capacity = self.capacity
        executor = self.executor
        if capacity is None or executor is None:  # pragma: no cover - policy invariant
            raise RuntimeError("compression executor is unavailable")
        if capacity.locked():
            self.stats.compression_rejected += 1
            raise _CompressionBusyError
        await capacity.acquire()
        source_fd: int | None = None
        try:
            if prepared.body is None:
                source_fd = os.dup(prepared.handle.fileno())
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                executor,
                _encode_prepared,
                self.cache,
                key,
                coding,
                prepared.body,
                source_fd,
                prepared.stat.st_size,
                self.policy.compression_delay,
            )
            source_fd = None  # worker owns the duplicate now
        except BaseException:
            if source_fd is not None:
                os.close(source_fd)
            capacity.release()
            raise
        self.stats.compression_submitted += 1
        self.futures.add(future)
        self.inflight[key] = future

        def finished(done: asyncio.Future[bytes]) -> None:
            self.futures.discard(done)
            if self.inflight.get(key) is done:
                self.inflight.pop(key, None)
            capacity.release()

        future.add_done_callback(finished)
        return await self._wait(future)

    async def close(self) -> None:
        if self.futures:
            await asyncio.gather(*tuple(self.futures), return_exceptions=True)
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)


def _hash_prepared(
    cache: _digest.DigestCache,
    key: _digest.CacheKey,
    algorithm: str,
    body: bytes | None,
    source_fd: int | None,
    expected_size: int,
    delay: float,
) -> str:
    """Worker-side digest over bytes or a cancellation-safe duplicate descriptor."""
    source = os.fdopen(source_fd, "rb") if source_fd is not None else None
    try:

        def produce() -> str:
            if delay:
                time.sleep(delay)
            if body is not None:
                if len(body) != expected_size:
                    raise OSError("buffered identity changed before hashing")
                return _digest.field_value(algorithm, body)
            if source is None:  # pragma: no cover - caller invariant
                raise RuntimeError("digest source is missing")
            return _digest.field_value_for_handle(source, algorithm, expected_size)

        return cache.get_or_compute(key, produce)
    finally:
        if source is not None:
            source.close()


class _DigestPlanner:
    """Optional cache-first, single-flight, bounded-executor representation hashing."""

    def __init__(self, policy: Policy, stats: Stats) -> None:
        self.policy = policy
        self.stats = stats
        self.cache = _digest.DigestCache(policy.digest_cache_size)
        self.executor = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=policy.digest_workers,
                thread_name_prefix="servery-selector-digest",
            )
            if policy.digest_workers
            else None
        )
        capacity = policy.digest_workers + policy.digest_queue
        self.capacity = asyncio.Semaphore(capacity) if capacity else None
        self.futures: set[asyncio.Future[str]] = set()
        self.inflight: dict[_digest.CacheKey, asyncio.Future[str]] = {}

    async def _wait(self, future: asyncio.Future[str]) -> str:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.stats.digest_cancelled += 1

            def completed_late(_done: asyncio.Future[str]) -> None:
                self.stats.digest_late_completed += 1

            future.add_done_callback(completed_late)
            raise
        except Exception as exc:
            self.stats.digest_errors += 1
            raise _DigestFailureError from exc

    async def value(self, prepared: _PreparedFile, algorithm: str) -> str | None:
        """Return a digest, or ``None`` when this prototype capability is disabled."""
        executor = self.executor
        if executor is None:
            return None
        key = _digest.cache_key(prepared.path, prepared.stat, algorithm)
        cached = self.cache.get(key)
        if cached is not None:
            self.stats.digest_hits += 1
            return cached
        existing = self.inflight.get(key)
        if existing is not None:
            self.stats.digest_shared += 1
            return await self._wait(existing)

        capacity = self.capacity
        if capacity is None:  # pragma: no cover - policy invariant
            raise RuntimeError("digest capacity is unavailable")
        if capacity.locked():
            self.stats.digest_rejected += 1
            raise _DigestBusyError
        await capacity.acquire()
        source_fd: int | None = None
        try:
            if prepared.body is None:
                source_fd = os.dup(prepared.handle.fileno())
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                executor,
                _hash_prepared,
                self.cache,
                key,
                algorithm,
                prepared.body,
                source_fd,
                prepared.stat.st_size,
                self.policy.digest_delay,
            )
            source_fd = None  # worker owns the duplicate now
        except BaseException:
            if source_fd is not None:
                os.close(source_fd)
            capacity.release()
            raise
        self.stats.digest_submitted += 1
        self.futures.add(future)
        self.inflight[key] = future

        def finished(done: asyncio.Future[str]) -> None:
            self.futures.discard(done)
            if self.inflight.get(key) is done:
                self.inflight.pop(key, None)
            capacity.release()

        future.add_done_callback(finished)
        return await self._wait(future)

    async def close(self) -> None:
        if self.futures:
            await asyncio.gather(*tuple(self.futures), return_exceptions=True)
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)


def _response_head(
    status: str,
    headers: Sequence[tuple[str, str]],
    *,
    close: bool,
) -> bytes:
    lines = [f"HTTP/1.1 {status}\r\n", "Server: servery-selector-prototype\r\n"]
    lines.append(f"Date: {_http1.http_date()}\r\n")
    lines.extend(f"{name}: {value}\r\n" for name, value in headers)
    if close:
        lines.append("Connection: close\r\n")
    lines.append("\r\n")
    return "".join(lines).encode("latin-1")


async def _drain(writer: asyncio.StreamWriter, timeout: float | None) -> None:
    if timeout is None:
        await writer.drain()
    else:
        await _write.drain(writer, timeout)


async def _send_error(
    writer: asyncio.StreamWriter,
    access: _RequestAccess,
    status: HTTPStatus,
    write_timeout: float | None,
    *,
    close: bool = True,
) -> None:
    headers = (("Content-Length", "0"), ("X-Content-Type-Options", "nosniff"))
    await access.record(status.value, headers)
    writer.write(
        _response_head(
            f"{status.value} {status.phrase}",
            headers,
            close=close,
        )
    )
    await _drain(writer, write_timeout)


async def _sendfile(
    writer: asyncio.StreamWriter,
    source: Any,
    offset: int,
    count: int,
    timeout: float | None,
) -> None:
    loop = asyncio.get_running_loop()
    if timeout is None:
        sent = await loop.sendfile(writer.transport, source, offset, count)
        if sent != count:
            raise ConnectionError(f"sendfile stopped after {sent} of {count} bytes")
        return
    sent_total = 0
    while sent_total < count:
        chunk_size = min(_SENDFILE_PROGRESS, count - sent_total)
        async with asyncio.timeout(timeout):
            sent = await loop.sendfile(
                writer.transport,
                source,
                offset + sent_total,
                chunk_size,
            )
        if sent <= 0:
            raise ConnectionError("sendfile made no progress")
        sent_total += sent


async def _serve_file(
    writer: asyncio.StreamWriter,
    access: _RequestAccess,
    root_real: str,
    filesystem: _FilesystemPlanner,
    listings: _ListingPlanner,
    compression: _CompressionPlanner,
    digest: _DigestPlanner,
    method: str,
    target: str,
    headers: _request.RequestHeaders,
    *,
    close: bool,
    write_timeout: float | None,
    cache_control: str,
) -> None:
    path = security.safe_join(root_real, urllib.parse.urlsplit(target).path)
    range_header = headers.get("Range")
    try:
        if path is None:
            raise FileNotFoundError
        prepared = await filesystem.prepare(
            root_real,
            path,
            target,
            method,
            headers.get("Accept-Encoding", ""),
            range_header is None,
        )
    except OSError:
        await _send_error(writer, access, HTTPStatus.NOT_FOUND, write_timeout, close=close)
        return

    if isinstance(prepared, _PreparedDirectory):
        if prepared.redirect is not None:
            response_headers = (
                ("Location", prepared.redirect),
                ("Content-Length", "0"),
                ("X-Content-Type-Options", "nosniff"),
            )
            await access.record(HTTPStatus.MOVED_PERMANENTLY.value, response_headers)
            writer.write(
                _response_head(
                    "301 Moved Permanently",
                    response_headers,
                    close=close,
                )
            )
            await _drain(writer, write_timeout)
            return
        try:
            generated = await listings.render(
                prepared.path,
                target,
                headers.get("Cookie"),
                headers.get("Accept-Encoding", ""),
            )
        except _ListingReadError:
            await _send_error(writer, access, HTTPStatus.NOT_FOUND, write_timeout, close=close)
            return
        if generated is None:
            await _send_error(
                writer,
                access,
                HTTPStatus.NOT_IMPLEMENTED,
                write_timeout,
                close=close,
            )
            return
        response_headers = (
            ("Content-Type", "text/html; charset=utf-8"),
            *(
                (("Content-Encoding", generated.encoding),)
                if generated.encoding is not None
                else ()
            ),
            ("Content-Length", str(len(generated.body))),
            *(
                (("Set-Cookie", generated.theme_cookie),)
                if generated.theme_cookie is not None
                else ()
            ),
            ("Vary", "Accept-Encoding"),
            ("Content-Security-Policy", _static.GENERATED_CSP),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
        )
        await access.record(HTTPStatus.OK.value, response_headers)
        response_head = _response_head("200 OK", response_headers, close=close)
        writer.write(response_head if method == "HEAD" else response_head + generated.body)
        await _drain(writer, write_timeout)
        return

    with contextlib.closing(prepared):
        if_none_match = headers.get("If-None-Match")
        if_modified_since = headers.get("If-Modified-Since")
        selection = (
            _static.select_identity(
                _static.FileBody(
                    prepared.path,
                    prepared.handle,
                    prepared.stat,
                    prepared.content_type,
                    prepared.coding,
                    prepared.etag,
                    prepared.last_modified,
                ),
                range_header=range_header,
                if_range=headers.get("If-Range"),
                if_none_match=if_none_match,
                if_modified_since=if_modified_since,
            )
            if range_header is not None
            or if_none_match is not None
            or if_modified_since is not None
            else None
        )
        vary_headers = (
            (("Vary", "Accept-Encoding"),) if _compress.compressible(prepared.content_type) else ()
        )
        validator_headers = (
            ("ETag", prepared.etag),
            ("Last-Modified", prepared.last_modified),
            *vary_headers,
            ("X-Content-Type-Options", "nosniff"),
        )
        if selection is not None and selection.status == 304:
            await access.record(HTTPStatus.NOT_MODIFIED.value, validator_headers)
            writer.write(_response_head("304 Not Modified", validator_headers, close=close))
            await _drain(writer, write_timeout)
            return
        if prepared.coding is not None:
            body = await compression.encode(prepared)
            response_headers = (
                ("Content-Type", prepared.content_type),
                ("Content-Encoding", prepared.coding),
                ("Content-Length", str(len(body))),
                *(
                    (
                        (
                            "Content-Disposition",
                            _static.content_disposition(Path(prepared.path).name),
                        ),
                    )
                    if _static.download_requested(target)
                    else ()
                ),
                ("Cache-Control", cache_control),
                *validator_headers,
            )
            await access.record(HTTPStatus.OK.value, response_headers)
            head = _response_head("200 OK", response_headers, close=close)
            writer.write(head if method == "HEAD" else head + body)
            await _drain(writer, write_timeout)
            return
        if selection is not None and selection.status == 416:
            response_headers = (
                ("Content-Range", selection.content_range or ""),
                ("Content-Length", "0"),
                ("Accept-Ranges", "bytes"),
                *vary_headers,
                ("X-Content-Type-Options", "nosniff"),
            )
            await access.record(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE.value, response_headers)
            writer.write(_response_head("416 Range Not Satisfiable", response_headers, close=close))
            await _drain(writer, write_timeout)
            return
        algorithm = _digest.choose_algorithm(headers.get("Want-Repr-Digest"))
        repr_digest = await digest.value(prepared, algorithm) if algorithm is not None else None
        response_headers = (
            ("Content-Type", prepared.content_type),
            (
                "Content-Length",
                str(prepared.stat.st_size if selection is None else selection.count),
            ),
            *(
                (
                    (
                        "Content-Disposition",
                        _static.content_disposition(Path(prepared.path).name),
                    ),
                )
                if _static.download_requested(target)
                else ()
            ),
            ("Accept-Ranges", "bytes"),
            ("Cache-Control", cache_control),
            *((("Repr-Digest", repr_digest),) if repr_digest is not None else ()),
            *validator_headers,
        )
        if selection is not None and selection.content_range is not None:
            response_headers = (
                *response_headers,
                ("Content-Range", selection.content_range),
            )
        status = (
            HTTPStatus.PARTIAL_CONTENT
            if selection is not None and selection.status == 206
            else HTTPStatus.OK
        )
        await access.record(status.value, response_headers)
        head = _response_head(f"{status.value} {status.phrase}", response_headers, close=close)
        offset = 0 if selection is None else selection.offset
        count = prepared.stat.st_size if selection is None else selection.count
        if method == "HEAD" or count == 0:
            writer.write(head)
            await _drain(writer, write_timeout)
        elif prepared.body is not None:
            end = offset + count
            writer.write(head + prepared.body[offset:end])
            await _drain(writer, write_timeout)
        else:
            writer.write(head)
            await _drain(writer, write_timeout)
            await _sendfile(
                writer,
                prepared.handle,
                offset,
                count,
                write_timeout,
            )


class _Connection:
    """Own one stream pair, parser remainder, and HTTP/1 lifecycle state."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        root_real: str,
        filesystem: _FilesystemPlanner,
        listings: _ListingPlanner,
        compression: _CompressionPlanner,
        digest: _DigestPlanner,
        access_logs: _AccessPlanner,
        policy: Policy,
        stats: Stats,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.root_real = root_real
        self.filesystem = filesystem
        self.listings = listings
        self.compression = compression
        self.digest = digest
        self.access_logs = access_logs
        self.policy = policy
        self.stats = stats
        self.pending = b""
        peer = writer.get_extra_info("peername")
        self.client = str(peer[0]) if isinstance(peer, tuple) and peer else "-"

    async def _read_head(self, timeout: float) -> _request.RequestHead | None:
        parser = _request.RequestHeadParser()
        async with asyncio.timeout(timeout):
            if self.pending:
                head, self.pending = parser.feed(self.pending)
                if parser.complete:
                    return head
            while True:
                data = await self.reader.read(_READ_SIZE)
                if not data:
                    return parser.finish()
                head, remainder = parser.feed(data)
                if parser.complete:
                    self.pending = remainder
                    return head

    async def run(self) -> None:
        requests = 0
        first = True
        while True:
            timeout = (
                self.policy.active_timeout
                if first or self.policy.keepalive_timeout is None
                else self.policy.keepalive_timeout
            )
            try:
                head = await self._read_head(timeout)
            except TimeoutError:
                self.stats.head_timeouts += 1
                self.writer.transport.abort()
                return
            except (
                _request.HeaderError,
                _request.RequestHeadError,
                _request.RequestLineError,
            ) as exc:
                self.stats.parse_errors += 1
                access = _RequestAccess(self.access_logs, self.client, "-", None)
                await _send_error(self.writer, access, exc.status, self.policy.write_timeout)
                return
            if head is None:
                return
            first = False
            requests += 1
            has_body = head.body.chunked or bool(head.body.length)
            close = (
                head.close_connection
                or has_body
                or (self.policy.max_requests > 0 and requests >= self.policy.max_requests)
            )
            method = head.request.method
            access = _RequestAccess(
                self.access_logs,
                self.client,
                head.request.requestline,
                head.headers,
            )
            if method not in {"GET", "HEAD"}:
                await _send_error(
                    self.writer,
                    access,
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    self.policy.write_timeout,
                )
                return
            try:
                await _serve_file(
                    self.writer,
                    access,
                    self.root_real,
                    self.filesystem,
                    self.listings,
                    self.compression,
                    self.digest,
                    method,
                    head.request.target,
                    head.headers,
                    close=close,
                    write_timeout=self.policy.write_timeout,
                    cache_control=self.policy.cache_control,
                )
            except (
                _FilesystemBusyError,
                _ListingBusyError,
                _CompressionBusyError,
                _DigestBusyError,
            ):
                await _send_error(
                    self.writer,
                    access,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    self.policy.write_timeout,
                )
                return
            except (_ListingFailureError, _CompressionFailureError, _DigestFailureError):
                await _send_error(
                    self.writer,
                    access,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    self.policy.write_timeout,
                )
                return
            except TimeoutError:
                self.stats.write_timeouts += 1
                self.writer.transport.abort()
                return
            self.stats.completed += 1
            if close:
                return


async def serve(
    directory: Path,
    host: str,
    port: int,
    *,
    policy: Policy | None = None,
    stats: Stats | None = None,
    started: Callable[[tuple[Any, ...]], None] | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Serve until cancelled or ``stop`` is set; drain active tasks on stop."""
    root_real = os.path.realpath(directory)
    policy = policy or Policy()
    stats = stats or Stats()
    filesystem = _FilesystemPlanner(policy, stats)
    listings = _ListingPlanner(policy, stats)
    compression = _CompressionPlanner(policy, stats)
    digest = _DigestPlanner(policy, stats)
    access_logs = _AccessPlanner(policy, stats)
    limit = (
        asyncio.Semaphore(policy.max_connections) if policy.max_connections is not None else None
    )
    active: set[asyncio.Task[None]] = set()

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if limit is not None:
            if limit.locked():
                stats.rejected += 1
                writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.wait_closed()
                return
            await limit.acquire()
        stats.accepted += 1
        try:
            await _Connection(
                reader,
                writer,
                root_real,
                filesystem,
                listings,
                compression,
                digest,
                access_logs,
                policy,
                stats,
            ).run()
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        except TimeoutError:
            stats.write_timeouts += 1
            writer.transport.abort()
        except ConnectionError:
            stats.transfer_errors += 1
            writer.transport.abort()
        except (OSError, ValueError):
            return
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            if limit is not None:
                limit.release()

    def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(connected(reader, writer))
        active.add(task)
        task.add_done_callback(active.discard)

    server = await asyncio.start_server(accepted, host, port, backlog=128)
    if started is not None:
        started(server.sockets[0].getsockname())
    try:
        if stop is None:
            await server.serve_forever()
        else:
            await stop.wait()
    finally:
        # Stop admission first, then drain/cancel owned connections. On newer
        # asyncio versions Server.wait_closed() may itself wait for client
        # callbacks, so awaiting it before task cancellation can deadlock drain.
        server.close()
        if active:
            _done, pending = await asyncio.wait(active, timeout=policy.drain_timeout)
            stats.cancelled += len(pending)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await compression.close()
        await digest.close()
        await listings.close()
        await filesystem.close()
        await access_logs.close()
        await server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keepalive-timeout", type=float)
    parser.add_argument("--write-timeout", type=float)
    parser.add_argument("--max-connections", type=int, default=256)
    parser.add_argument("--max-requests-per-connection", type=int, default=0)
    parser.add_argument("--drain-timeout", type=float, default=5.0)
    parser.add_argument("--small-file-buffer-size", type=int, default=_SMALL_BODY)
    parser.add_argument("--filesystem-workers", type=int, default=0)
    parser.add_argument("--filesystem-queue", type=int, default=0)
    parser.add_argument("--filesystem-delay-ms", type=float, default=0.0)
    parser.add_argument("--listing-workers", type=int, default=0)
    parser.add_argument("--listing-queue", type=int, default=0)
    parser.add_argument("--listing-delay-ms", type=float, default=0.0)
    parser.add_argument("--show-hidden", action="store_true")
    parser.add_argument("--max-listing-entries", type=int, default=100_000)
    parser.add_argument("--listing-page-size", type=int, default=1000)
    parser.add_argument("--listing-details-threshold", type=int, default=10_000)
    parser.add_argument("--cache-control", default="no-cache")
    parser.add_argument("--spa", action="store_true")
    parser.add_argument("--compress", action="store_true")
    parser.add_argument("--max-compress-size", type=int, default=_compress.GZIP_MAX)
    parser.add_argument("--compression-cache-size", type=int, default=0)
    parser.add_argument("--compression-workers", type=int, default=0)
    parser.add_argument("--compression-queue", type=int, default=0)
    parser.add_argument("--compression-delay-ms", type=float, default=0.0)
    parser.add_argument("--digest-cache-size", type=int, default=0)
    parser.add_argument("--digest-workers", type=int, default=0)
    parser.add_argument("--digest-queue", type=int, default=0)
    parser.add_argument("--digest-delay-ms", type=float, default=0.0)
    parser.add_argument("--access-log")
    parser.add_argument("--access-log-format", choices=("clf", "combined", "json"), default="clf")
    parser.add_argument("--access-log-queue", type=int, default=256)
    parser.add_argument("--access-log-overflow", choices=("drop", "wait"), default="drop")
    parser.add_argument("--access-log-batch-size", type=int, default=8)
    parser.add_argument("--access-log-batch-wait-ms", type=float, default=1.0)
    parser.add_argument("--access-log-delay-ms", type=float, default=0.0)
    args = parser.parse_args()
    policy = Policy(
        active_timeout=args.timeout,
        keepalive_timeout=args.keepalive_timeout,
        write_timeout=args.write_timeout,
        max_connections=args.max_connections,
        max_requests=args.max_requests_per_connection,
        drain_timeout=args.drain_timeout,
        small_file_buffer_size=args.small_file_buffer_size,
        filesystem_workers=args.filesystem_workers,
        filesystem_queue=args.filesystem_queue,
        filesystem_delay=args.filesystem_delay_ms / 1000,
        listing_workers=args.listing_workers,
        listing_queue=args.listing_queue,
        listing_delay=args.listing_delay_ms / 1000,
        show_hidden=args.show_hidden,
        max_listing_entries=args.max_listing_entries,
        listing_page_size=args.listing_page_size,
        listing_details_threshold=args.listing_details_threshold,
        cache_control=args.cache_control,
        spa=args.spa,
        compress=args.compress,
        max_compress_size=args.max_compress_size,
        compression_cache_size=args.compression_cache_size,
        compression_workers=args.compression_workers,
        compression_queue=args.compression_queue,
        compression_delay=args.compression_delay_ms / 1000,
        digest_cache_size=args.digest_cache_size,
        digest_workers=args.digest_workers,
        digest_queue=args.digest_queue,
        digest_delay=args.digest_delay_ms / 1000,
        access_log=args.access_log,
        access_log_format=args.access_log_format,
        access_log_queue=args.access_log_queue,
        access_log_overflow=args.access_log_overflow,
        access_log_batch_size=args.access_log_batch_size,
        access_log_batch_wait=args.access_log_batch_wait_ms / 1000,
        access_log_delay=args.access_log_delay_ms / 1000,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.directory, args.bind, args.port, policy=policy))


if __name__ == "__main__":
    main()
