from __future__ import annotations

import asyncio
import threading
import unittest
from unittest import mock

from servery._work import (
    BoundedWorkLimiter,
    BoundedWorkPool,
    WorkClass,
    WorkLanePolicy,
    WorkPoolClosedError,
    WorkRejectedError,
    WorkRejectReason,
    WorkScheduler,
)


class BoundedWorkPoolTest(unittest.TestCase):
    def test_invalid_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BoundedWorkPool("", workers=1, queue_capacity=0)
        with self.assertRaises(ValueError):
            BoundedWorkPool("x", workers=0, queue_capacity=0)
        with self.assertRaises(ValueError):
            BoundedWorkPool("x", workers=1, queue_capacity=-1)
        with self.assertRaises(ValueError):
            BoundedWorkPool("x", workers=1, queue_capacity=0, byte_capacity=-1)

    def test_exact_capacity_rejects_without_invoking_and_recovers(self) -> None:
        pool = BoundedWorkPool("test", workers=1, queue_capacity=1)
        entered = threading.Event()
        release = threading.Event()
        invoked: list[int] = []

        def blocked(value: int) -> int:
            invoked.append(value)
            entered.set()
            release.wait(2)
            return value

        first = pool.submit(blocked, 1)
        self.assertTrue(entered.wait(1))
        second = pool.submit(blocked, 2)
        with self.assertRaises(WorkRejectedError):
            pool.submit(blocked, 3)
        self.assertEqual(invoked, [1])
        snapshot = pool.snapshot()
        self.assertEqual((snapshot.active, snapshot.queued, snapshot.high_water), (1, 1, 2))
        self.assertEqual(snapshot.rejected, 1)

        release.set()
        self.assertEqual(first.result(1), 1)
        self.assertEqual(second.result(1), 2)
        self.assertEqual(pool.submit(lambda: 4).result(1), 4)
        pool.close(wait=True)
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.submitted, 3)
        self.assertEqual(snapshot.succeeded, 3)
        self.assertEqual(snapshot.active, 0)
        self.assertEqual(snapshot.queued, 0)

    def test_weighted_admission_rejects_by_bytes_and_recovers(self) -> None:
        pool = BoundedWorkPool("test", workers=2, queue_capacity=1, byte_capacity=10)
        entered = threading.Event()
        release = threading.Event()

        def blocked() -> bytes:
            entered.set()
            release.wait(2)
            return b"ok"

        first = pool.submit_weighted(7, blocked)
        self.assertTrue(entered.wait(1))
        with self.assertRaises(WorkRejectedError) as caught:
            pool.submit_weighted(4, blocked)
        self.assertEqual(caught.exception.reason, WorkRejectReason.BYTE_BUDGET)
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.bytes_reserved, 7)
        self.assertEqual(snapshot.bytes_high_water, 7)
        self.assertEqual(snapshot.rejected_bytes, 1)
        self.assertEqual(snapshot.rejected_capacity, 0)
        release.set()
        first.result(1)
        self.assertEqual(pool.submit_weighted(10, lambda: b"next").result(1), b"next")
        pool.close(wait=True)
        self.assertEqual(pool.snapshot().bytes_reserved, 0)
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.submitted, 2)
        self.assertEqual(snapshot.succeeded, 2)
        self.assertEqual(snapshot.active, 0)
        self.assertEqual(snapshot.queued, 0)

    def test_worker_failure_releases_capacity(self) -> None:
        pool = BoundedWorkPool("test", workers=1, queue_capacity=0)

        def fail() -> None:
            raise OSError("boom")

        with self.assertRaises(OSError):
            pool.submit(fail).result(1)
        self.assertEqual(pool.submit(lambda: "ok").result(1), "ok")
        pool.close(wait=True)
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.failed, 1)
        self.assertEqual(snapshot.succeeded, 1)

    def test_queued_cancellation_releases_only_when_future_is_done(self) -> None:
        pool = BoundedWorkPool("test", workers=1, queue_capacity=1)
        entered = threading.Event()
        release = threading.Event()
        first = pool.submit(lambda: (entered.set(), release.wait(2)))
        self.assertTrue(entered.wait(1))
        queued = pool.submit(lambda: None)
        self.assertTrue(queued.cancel())
        replacement = pool.submit(lambda: "replacement")
        release.set()
        first.result(1)
        self.assertEqual(replacement.result(1), "replacement")
        pool.close(wait=True)
        self.assertEqual(pool.snapshot().cancelled, 1)

    def test_close_is_idempotent_and_rejects_new_work(self) -> None:
        pool = BoundedWorkPool("test", workers=1, queue_capacity=0)
        pool.close(wait=True)
        pool.close(wait=True)
        self.assertTrue(pool.snapshot().closed)
        with self.assertRaises(WorkPoolClosedError):
            pool.submit(lambda: None)

    def test_submit_failure_releases_permit(self) -> None:
        pool = BoundedWorkPool("test", workers=1, queue_capacity=0)
        with (
            mock.patch.object(pool._executor, "submit", side_effect=RuntimeError("closed")),
            self.assertRaises(RuntimeError),
        ):
            pool.submit(lambda: None)
        # The failed handoff did not consume the only admission permit.
        self.assertTrue(pool._permits.acquire(blocking=False))
        pool._permits.release()
        with self.assertRaises(ValueError):
            pool.submit_weighted(-1, lambda: None)
        pool.close(wait=True)


class BoundedWorkLimiterTest(unittest.TestCase):
    def test_inline_leases_bound_jobs_and_bytes(self) -> None:
        limiter = BoundedWorkLimiter("compute", active=2, byte_capacity=10)
        first = limiter.reserve(weight_bytes=7)
        with self.assertRaises(WorkRejectedError) as caught:
            limiter.reserve(weight_bytes=4)
        self.assertEqual(caught.exception.reason, WorkRejectReason.BYTE_BUDGET)
        second = limiter.reserve(weight_bytes=3)
        with self.assertRaises(WorkRejectedError) as caught:
            limiter.reserve()
        self.assertEqual(caught.exception.reason, WorkRejectReason.CAPACITY)
        snapshot = limiter.snapshot()
        self.assertEqual((snapshot.active, snapshot.bytes_reserved), (2, 10))
        first.release()
        first.release()
        second.release(failed=True)
        snapshot = limiter.snapshot()
        self.assertEqual(snapshot.active, 0)
        self.assertEqual(snapshot.bytes_reserved, 0)
        self.assertEqual(snapshot.succeeded, 1)
        self.assertEqual(snapshot.failed, 1)
        self.assertEqual(snapshot.rejected_bytes, 1)
        self.assertEqual(snapshot.rejected_capacity, 1)

    def test_context_manager_and_close(self) -> None:
        limiter = BoundedWorkLimiter("compute", active=1)
        with limiter.reserve():
            self.assertEqual(limiter.snapshot().active, 1)
        limiter.close()
        self.assertTrue(limiter.snapshot().closed)
        with self.assertRaises(WorkPoolClosedError):
            limiter.reserve()

    def test_concurrent_duplicate_release_is_idempotent(self) -> None:
        limiter = BoundedWorkLimiter("compute", active=1)
        lease = limiter.reserve(weight_bytes=1)
        threads = [threading.Thread(target=lease.release) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)
        snapshot = limiter.snapshot()
        self.assertEqual(snapshot.active, 0)
        self.assertEqual(snapshot.bytes_reserved, 0)
        self.assertEqual(snapshot.succeeded, 1)

    def test_invalid_policy(self) -> None:
        with self.assertRaises(ValueError):
            BoundedWorkLimiter("", active=1)
        with self.assertRaises(ValueError):
            BoundedWorkLimiter("x", active=0)
        with self.assertRaises(ValueError):
            BoundedWorkLimiter("x", active=1, byte_capacity=-1)
        limiter = BoundedWorkLimiter("x", active=1)
        with self.assertRaises(ValueError):
            limiter.reserve(weight_bytes=-1)


class AsyncBoundedWorkPoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_running_work_keeps_capacity_and_disposes_late_result(self) -> None:
        pool = BoundedWorkPool("test", workers=1, queue_capacity=0)
        entered = threading.Event()
        release = threading.Event()
        disposed: list[object] = []
        result = object()

        def blocked() -> object:
            entered.set()
            release.wait(2)
            return result

        task = asyncio.create_task(pool.run_async(blocked, abandon_result=disposed.append))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(WorkRejectedError):
            pool.submit(lambda: None)
        release.set()
        for _ in range(100):
            if disposed:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(disposed, [result])
        self.assertEqual(pool.submit(lambda: "recovered").result(1), "recovered")
        pool.close(wait=True)

    async def test_cancelled_queued_work_never_runs(self) -> None:
        pool = BoundedWorkPool("test", workers=1, queue_capacity=1)
        entered = threading.Event()
        release = threading.Event()
        invoked = False

        def blocked() -> None:
            entered.set()
            release.wait(2)

        def queued() -> None:
            nonlocal invoked
            invoked = True

        running = pool.submit(blocked)
        await asyncio.to_thread(entered.wait, 1)
        task = asyncio.create_task(pool.run_async(queued))
        for _ in range(100):
            if pool.snapshot().queued:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        running.result(1)
        self.assertFalse(invoked)
        self.assertEqual(pool.snapshot().cancelled, 1)
        pool.close(wait=True)


class WorkSchedulerTest(unittest.TestCase):
    def test_requires_a_lane_and_rejects_unconfigured_lane(self) -> None:
        with self.assertRaises(ValueError):
            WorkScheduler({})
        scheduler = WorkScheduler({WorkClass.COMPUTE: WorkLanePolicy(1)})
        try:
            with self.assertRaisesRegex(ValueError, "filesystem"):
                scheduler.submit(WorkClass.FILESYSTEM, lambda: None)
        finally:
            scheduler.close(wait=True)

    def test_expensive_saturation_does_not_starve_filesystem_lane(self) -> None:
        scheduler = WorkScheduler(
            {
                WorkClass.FILESYSTEM: WorkLanePolicy(1),
                WorkClass.COMPUTE: WorkLanePolicy(1, queue_capacity=1, byte_capacity=8),
            }
        )
        entered = threading.Event()
        release = threading.Event()

        def blocked() -> None:
            entered.set()
            release.wait(2)

        running = scheduler.submit(WorkClass.COMPUTE, blocked)
        self.assertTrue(entered.wait(1))
        queued = scheduler.submit(WorkClass.COMPUTE, blocked)
        with self.assertRaises(WorkRejectedError):
            scheduler.submit(WorkClass.COMPUTE, blocked)
        self.assertEqual(
            scheduler.submit(WorkClass.FILESYSTEM, lambda: "cheap").result(1),
            "cheap",
        )
        release.set()
        running.result(1)
        queued.result(1)
        snapshots = scheduler.snapshots()
        self.assertEqual(snapshots[WorkClass.COMPUTE].rejected, 1)
        self.assertEqual(snapshots[WorkClass.FILESYSTEM].succeeded, 1)
        scheduler.close(wait=True)

    def test_scheduler_routes_weighted_admission(self) -> None:
        scheduler = WorkScheduler({WorkClass.COMPUTE: WorkLanePolicy(1, byte_capacity=4)})
        try:
            with self.assertRaises(WorkRejectedError) as caught:
                scheduler.submit_weighted(WorkClass.COMPUTE, 5, lambda: None)
            self.assertEqual(caught.exception.reason, WorkRejectReason.BYTE_BUDGET)
            with self.assertRaisesRegex(ValueError, "filesystem"):
                scheduler.submit_weighted(WorkClass.FILESYSTEM, 0, lambda: None)
        finally:
            scheduler.close(wait=True)

    def test_scheduler_async_adapter_and_missing_lane(self) -> None:
        async def exercise() -> None:
            scheduler = WorkScheduler({WorkClass.COMPUTE: WorkLanePolicy(1)})
            try:
                result = await scheduler.run_async(WorkClass.COMPUTE, lambda: "ok")
                self.assertEqual(result, "ok")
                with self.assertRaisesRegex(ValueError, "filesystem"):
                    await scheduler.run_async(WorkClass.FILESYSTEM, lambda: None)
            finally:
                scheduler.close(wait=True)

        asyncio.run(exercise())
