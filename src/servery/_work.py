"""Bounded ownership primitives for blocking request work.

``ThreadPoolExecutor`` limits running threads but deliberately leaves its input
queue unbounded.  :class:`BoundedWorkPool` adds non-blocking admission around an
owned executor and keeps capacity until a submitted call actually finishes.  A
cancelled waiter therefore cannot make a still-running filesystem or CPU job
disappear from the resource budget.

The pool is intentionally smaller than a request scheduler.  Callers choose a
lane (for example cheap metadata or expensive compression) and retain ownership
of request-specific resources.  In particular, the pool never materializes a
streaming response merely to move it between threads.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

_T = TypeVar("_T")


class WorkRejectedError(RuntimeError):
    """A bounded work lane rejected a call for one stable reason."""

    def __init__(self, message: str, reason: WorkRejectReason) -> None:
        super().__init__(message)
        self.reason = reason


class WorkPoolClosedError(RuntimeError):
    """Work was submitted after a pool stopped admission."""


class WorkRejectReason(StrEnum):
    """Stable bounded-cardinality reasons for overload accounting."""

    CAPACITY = "capacity"
    BYTE_BUDGET = "byte_budget"


class WorkClass(StrEnum):
    """Logical lanes whose physical separation preserves cheap-work progress."""

    FILESYSTEM = "filesystem"
    COMPUTE = "compute"
    STREAM = "stream"
    APPLICATION = "application"


@dataclass(frozen=True, slots=True)
class WorkLanePolicy:
    """Per-worker capacity for one blocking-work lane."""

    workers: int
    queue_capacity: int = 0
    byte_capacity: int | None = None


@dataclass(frozen=True, slots=True)
class WorkSnapshot:
    """One bounded-cardinality view of a work pool's lifecycle counters."""

    capacity: int
    byte_capacity: int | None
    bytes_reserved: int
    bytes_high_water: int
    active: int
    queued: int
    high_water: int
    submitted: int
    rejected: int
    rejected_capacity: int
    rejected_bytes: int
    succeeded: int
    failed: int
    cancelled: int
    closed: bool


class WorkLease:
    """One idempotently released inline-work reservation."""

    def __init__(self, limiter: BoundedWorkLimiter, weight_bytes: int) -> None:
        self._limiter = limiter
        self._weight_bytes = weight_bytes
        self._lock = threading.Lock()
        self._released = False

    def __enter__(self) -> WorkLease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release(failed=exc_type is not None)

    def release(self, *, failed: bool = False) -> None:
        """Release once; duplicate cleanup is harmless."""
        with self._lock:
            if self._released:
                return
            self._released = True
        self._limiter._release(self._weight_bytes, failed=failed)


class BoundedWorkLimiter:
    """Non-blocking leases for work that stays on a connection worker."""

    def __init__(self, name: str, *, active: int, byte_capacity: int | None = None) -> None:
        if not name:
            raise ValueError("work limiter name cannot be empty")
        if active <= 0:
            raise ValueError("active must be positive")
        if byte_capacity is not None and byte_capacity < 0:
            raise ValueError("byte_capacity cannot be negative")
        self.name = name
        self.capacity = active
        self.byte_capacity = byte_capacity
        self._condition = threading.Condition()
        self._closed = False
        self._active = 0
        self._high_water = 0
        self._bytes_reserved = 0
        self._bytes_high_water = 0
        self._submitted = 0
        self._rejected = 0
        self._rejected_capacity = 0
        self._rejected_bytes = 0
        self._succeeded = 0
        self._failed = 0

    def reserve(self, *, weight_bytes: int = 0) -> WorkLease:
        """Reserve inline capacity or reject immediately."""
        if weight_bytes < 0:
            raise ValueError("weight_bytes cannot be negative")
        with self._condition:
            if self._closed:
                raise WorkPoolClosedError(f"work limiter {self.name!r} is closed")
            if self._active >= self.capacity:
                self._rejected += 1
                self._rejected_capacity += 1
                raise WorkRejectedError(
                    f"work limiter {self.name!r} is saturated",
                    WorkRejectReason.CAPACITY,
                )
            if (
                self.byte_capacity is not None
                and self._bytes_reserved + weight_bytes > self.byte_capacity
            ):
                self._rejected += 1
                self._rejected_bytes += 1
                raise WorkRejectedError(
                    f"work limiter {self.name!r} byte budget is saturated",
                    WorkRejectReason.BYTE_BUDGET,
                )
            self._active += 1
            self._high_water = max(self._high_water, self._active)
            self._bytes_reserved += weight_bytes
            self._bytes_high_water = max(self._bytes_high_water, self._bytes_reserved)
            self._submitted += 1
        return WorkLease(self, weight_bytes)

    def _release(self, weight_bytes: int, *, failed: bool) -> None:
        with self._condition:
            self._active -= 1
            self._bytes_reserved -= weight_bytes
            if failed:
                self._failed += 1
            else:
                self._succeeded += 1
            self._condition.notify_all()

    def snapshot(self) -> WorkSnapshot:
        """Return the same metric shape as executor-backed work lanes."""
        with self._condition:
            return WorkSnapshot(
                capacity=self.capacity,
                byte_capacity=self.byte_capacity,
                bytes_reserved=self._bytes_reserved,
                bytes_high_water=self._bytes_high_water,
                active=self._active,
                queued=0,
                high_water=self._high_water,
                submitted=self._submitted,
                rejected=self._rejected,
                rejected_capacity=self._rejected_capacity,
                rejected_bytes=self._rejected_bytes,
                succeeded=self._succeeded,
                failed=self._failed,
                cancelled=0,
                closed=self._closed,
            )

    def close(self) -> None:
        """Stop new inline reservations without interrupting active work."""
        with self._condition:
            self._closed = True


class BoundedWorkPool:
    """An owned thread pool with exact, non-blocking submission capacity."""

    def __init__(
        self,
        name: str,
        *,
        workers: int,
        queue_capacity: int,
        byte_capacity: int | None = None,
    ) -> None:
        if not name:
            raise ValueError("work pool name cannot be empty")
        if workers <= 0:
            raise ValueError("workers must be positive")
        if queue_capacity < 0:
            raise ValueError("queue_capacity cannot be negative")
        if byte_capacity is not None and byte_capacity < 0:
            raise ValueError("byte_capacity cannot be negative")
        self.name = name
        self.workers = workers
        self.queue_capacity = queue_capacity
        self.capacity = workers + queue_capacity
        self.byte_capacity = byte_capacity
        self._permits = threading.BoundedSemaphore(self.capacity)
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"servery-{name}",
        )
        self._condition = threading.Condition()
        self._futures: set[Future[Any]] = set()
        self._closed = False
        self._active = 0
        self._high_water = 0
        self._bytes_reserved = 0
        self._bytes_high_water = 0
        self._submitted = 0
        self._rejected = 0
        self._rejected_capacity = 0
        self._rejected_bytes = 0
        self._succeeded = 0
        self._failed = 0
        self._cancelled = 0

    def submit(self, function: Callable[..., _T], /, *args: Any, **kwargs: Any) -> Future[_T]:
        """Submit one call or reject immediately without invoking ``function``."""
        return self.submit_weighted(0, function, *args, **kwargs)

    def submit_weighted(
        self,
        weight_bytes: int,
        function: Callable[..., _T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[_T]:
        """Submit work while retaining a declared byte budget until completion."""
        if weight_bytes < 0:
            raise ValueError("weight_bytes cannot be negative")
        with self._condition:
            if self._closed:
                raise WorkPoolClosedError(f"work pool {self.name!r} is closed")
        if not self._permits.acquire(blocking=False):
            with self._condition:
                self._rejected += 1
                self._rejected_capacity += 1
            raise WorkRejectedError(
                f"work pool {self.name!r} is saturated",
                WorkRejectReason.CAPACITY,
            )
        with self._condition:
            if self._closed:
                self._permits.release()
                raise WorkPoolClosedError(f"work pool {self.name!r} is closed")
            if (
                self.byte_capacity is not None
                and self._bytes_reserved + weight_bytes > self.byte_capacity
            ):
                self._rejected += 1
                self._rejected_bytes += 1
                self._permits.release()
                raise WorkRejectedError(
                    f"work pool {self.name!r} byte budget is saturated",
                    WorkRejectReason.BYTE_BUDGET,
                )
            self._bytes_reserved += weight_bytes
            self._bytes_high_water = max(self._bytes_high_water, self._bytes_reserved)

        def invoke() -> _T:
            with self._condition:
                self._active += 1
            try:
                result = function(*args, **kwargs)
            except BaseException:
                with self._condition:
                    self._failed += 1
                raise
            else:
                with self._condition:
                    self._succeeded += 1
                return result
            finally:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

        try:
            future = self._executor.submit(invoke)
        except BaseException:
            with self._condition:
                self._bytes_reserved -= weight_bytes
            self._permits.release()
            raise
        with self._condition:
            self._submitted += 1
            self._futures.add(future)
            self._high_water = max(self._high_water, len(self._futures))

        def finished(done: Future[_T]) -> None:
            with self._condition:
                self._futures.discard(done)
                self._bytes_reserved -= weight_bytes
                if done.cancelled():
                    self._cancelled += 1
                self._condition.notify_all()
            self._permits.release()

        future.add_done_callback(finished)
        return future

    async def run_async(
        self,
        function: Callable[..., _T],
        /,
        *args: Any,
        abandon_result: Callable[[_T], None] | None = None,
        **kwargs: Any,
    ) -> _T:
        """Run admitted work without releasing its permit on waiter cancellation.

        A queued call is cancelled when possible.  A call that has started keeps
        its permit until completion.  ``abandon_result`` transfers ownership of a
        successful late result (for example, an opened file handle) back to the
        caller's cleanup function.
        """
        future = self.submit(function, *args, **kwargs)
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            if not future.cancel() and abandon_result is not None:

                def dispose(done: Future[_T]) -> None:
                    if not done.cancelled() and done.exception() is None:
                        abandon_result(done.result())

                future.add_done_callback(dispose)
            raise

    def snapshot(self) -> WorkSnapshot:
        """Return counters without exposing executor or semaphore internals."""
        with self._condition:
            inflight = len(self._futures)
            return WorkSnapshot(
                capacity=self.capacity,
                byte_capacity=self.byte_capacity,
                bytes_reserved=self._bytes_reserved,
                bytes_high_water=self._bytes_high_water,
                active=self._active,
                queued=max(0, inflight - self._active),
                high_water=self._high_water,
                submitted=self._submitted,
                rejected=self._rejected,
                rejected_capacity=self._rejected_capacity,
                rejected_bytes=self._rejected_bytes,
                succeeded=self._succeeded,
                failed=self._failed,
                cancelled=self._cancelled,
                closed=self._closed,
            )

    def close(self, *, wait: bool, cancel_queued: bool = True) -> None:
        """Stop admission and optionally wait for calls that cannot be cancelled."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=cancel_queued)


class WorkScheduler:
    """Route named work classes to physically separate bounded executors."""

    def __init__(self, policies: dict[WorkClass, WorkLanePolicy]) -> None:
        if not policies:
            raise ValueError("at least one work lane is required")
        self._pools = {
            work_class: BoundedWorkPool(
                f"work-{work_class.value}",
                workers=policy.workers,
                queue_capacity=policy.queue_capacity,
                byte_capacity=policy.byte_capacity,
            )
            for work_class, policy in policies.items()
        }

    def submit(
        self,
        work_class: WorkClass,
        function: Callable[..., _T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[_T]:
        """Submit to one lane, failing at the call site if it is not configured."""
        try:
            pool = self._pools[work_class]
        except KeyError:
            raise ValueError(f"work lane {work_class.value!r} is not configured") from None
        return pool.submit(function, *args, **kwargs)

    def submit_weighted(
        self,
        work_class: WorkClass,
        weight_bytes: int,
        function: Callable[..., _T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[_T]:
        """Submit to one lane while charging retained input/output bytes."""
        try:
            pool = self._pools[work_class]
        except KeyError:
            raise ValueError(f"work lane {work_class.value!r} is not configured") from None
        return pool.submit_weighted(weight_bytes, function, *args, **kwargs)

    async def run_async(
        self,
        work_class: WorkClass,
        function: Callable[..., _T],
        /,
        *args: Any,
        abandon_result: Callable[[_T], None] | None = None,
        **kwargs: Any,
    ) -> _T:
        """Await one lane while retaining late-result ownership semantics."""
        try:
            pool = self._pools[work_class]
        except KeyError:
            raise ValueError(f"work lane {work_class.value!r} is not configured") from None
        return await pool.run_async(
            function,
            *args,
            abandon_result=abandon_result,
            **kwargs,
        )

    def snapshots(self) -> dict[WorkClass, WorkSnapshot]:
        """Return one snapshot per configured lane."""
        return {work_class: pool.snapshot() for work_class, pool in self._pools.items()}

    def close(self, *, wait: bool, cancel_queued: bool = True) -> None:
        """Stop admission on every lane before closing any executor."""
        pools = tuple(self._pools.values())
        for pool in pools:
            with pool._condition:
                pool._closed = True
        for pool in pools:
            pool._executor.shutdown(wait=wait, cancel_futures=cancel_queued)
