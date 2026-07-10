"""Deterministic per-target serialization and concurrent upload tests."""

from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from servery import _resumable, _writecoord
from servery.config import Config
from tests._harness import serving


class TargetLocksTest(unittest.TestCase):
    def test_canonical_aliases_conflict_and_registry_is_reclaimed(self):
        locks = _writecoord.TargetLocks()
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "file.bin")
            alias = str(Path(tmp) / "sub" / ".." / "file.bin")
            attempted = threading.Event()
            result: list[bool] = []

            def contender() -> None:
                with locks.hold(alias) as acquired:
                    result.append(acquired)
                    attempted.set()

            with locks.hold(target) as acquired:
                self.assertTrue(acquired)
                thread = threading.Thread(target=contender)
                thread.start()
                self.assertTrue(attempted.wait(2))
                thread.join(2)
            self.assertEqual(result, [False])
            self.assertEqual(locks._entries, {})

    def test_hold_many_uses_stable_order_and_releases_every_entry(self):
        locks = _writecoord.TargetLocks()
        with locks.hold_many(["b", "a", "b"]) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(len(locks._entries), 2)
        self.assertEqual(locks._entries, {})

    def test_hold_many_releases_partial_acquisition_on_conflict(self):
        locks = _writecoord.TargetLocks()
        ready = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with locks.hold("b") as acquired:
                self.assertTrue(acquired)
                ready.set()
                release.wait(2)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(ready.wait(2))
        with locks.hold_many(["a", "b"]) as acquired:
            self.assertFalse(acquired)
        release.set()
        thread.join(2)
        self.assertEqual(locks._entries, {})


class ConcurrentWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = Config.create(
            self.root,
            host="127.0.0.1",
            port=0,
            quiet=True,
            upload=True,
            # Both 512 KiB contenders should receive an HTTP decision.  Production
            # users can keep the lower default to cap rejection-drain work.
            keepalive_drain_limit=1024 * 1024,
        )

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _multipart(payload: bytes) -> bytes:
        return (
            b'--B\r\nContent-Disposition: form-data; name="f"; filename="same.bin"\r\n'
            b"\r\n" + payload + b"\r\n--B--\r\n"
        )

    def test_concurrent_multipart_no_overwrite_has_one_winner(self):
        barrier = threading.Barrier(3)
        results: list[tuple[int, bytes]] = []

        with serving(self.cfg) as (host, port):

            def upload(payload: bytes) -> None:
                body = self._multipart(payload)
                barrier.wait()
                conn = http.client.HTTPConnection(host, port, timeout=10)
                try:
                    conn.request(
                        "POST",
                        "/",
                        body=body,
                        headers={"Content-Type": "multipart/form-data; boundary=B"},
                    )
                    response = conn.getresponse()
                    response.read()
                    results.append((response.status, payload))
                finally:
                    conn.close()

            payloads = (b"A" * 512_000, b"B" * 512_000)
            threads = [threading.Thread(target=upload, args=(payload,)) for payload in payloads]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(15)

        self.assertEqual(sorted(status for status, _ in results), [303, 409])
        winner = next(payload for status, payload in results if status == 303)
        self.assertEqual((self.root / "same.bin").read_bytes(), winner)

    def test_concurrent_resumable_chunks_cannot_overlap(self):
        barrier = threading.Barrier(3)
        results: list[tuple[int, bytes]] = []
        payloads = (b"A" * 512_000, b"B" * 512_000)

        with serving(self.cfg) as (host, port):

            def put(payload: bytes) -> None:
                barrier.wait()
                conn = http.client.HTTPConnection(host, port, timeout=10)
                try:
                    conn.request(
                        "PUT",
                        "/partial.bin",
                        body=payload,
                        headers={"Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload) * 2}"},
                    )
                    response = conn.getresponse()
                    response.read()
                    results.append((response.status, payload))
                finally:
                    conn.close()

            threads = [threading.Thread(target=put, args=(payload,)) for payload in payloads]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(15)

        self.assertEqual(sorted(status for status, _ in results), [308, 409])
        winner = next(payload for status, payload in results if status == 308)
        part = self.root / f".partial.bin{_resumable.PART_SUFFIX}"
        self.assertEqual(part.read_bytes(), winner)


if __name__ == "__main__":
    unittest.main()
