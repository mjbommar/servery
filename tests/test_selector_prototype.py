from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import os
import socket
import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from benchmarks.comparison import selector_prototype
from benchmarks.comparison.selector_prototype import Policy, Stats, _prepare_file, serve

from servery import _digest, _static, listing


@asynccontextmanager
async def _running(
    directory: Path,
    policy: Policy,
) -> AsyncIterator[tuple[str, int, Stats, asyncio.Event]]:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[tuple[str, int]] = loop.create_future()
    stop = asyncio.Event()
    stats = Stats()

    def started(address: tuple[Any, ...]) -> None:
        ready.set_result((str(address[0]), int(address[1])))

    task = asyncio.create_task(
        serve(directory, "127.0.0.1", 0, policy=policy, stats=stats, started=started, stop=stop)
    )
    host, port = await asyncio.wait_for(ready, 2)
    try:
        yield host, port, stats, stop
    finally:
        stop.set()
        await asyncio.wait_for(task, 3)


async def _exchange(host: str, port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(), 2)
    finally:
        writer.close()
        await writer.wait_closed()


def _response_header(response: bytes, name: bytes) -> bytes | None:
    head = response.split(b"\r\n\r\n", 1)[0]
    prefix = name.lower() + b":"
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(prefix):
            return line.split(b":", 1)[1].strip()
    return None


class SelectorPrototypeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "small.txt").write_bytes(b"payload")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_policy_rejects_invalid_resource_values(self) -> None:
        invalid_values: tuple[dict[str, Any], ...] = (
            {"active_timeout": 0},
            {"keepalive_timeout": 0},
            {"write_timeout": 0},
            {"max_connections": 0},
            {"max_requests": -1},
            {"drain_timeout": -1},
            {"small_file_buffer_size": -1},
            {"filesystem_workers": -1},
            {"filesystem_queue": -1},
            {"filesystem_queue": 1},
            {"filesystem_delay": -1},
            {"listing_workers": -1},
            {"listing_queue": -1},
            {"listing_queue": 1},
            {"listing_delay": -1},
            {"max_listing_entries": 0},
            {"listing_page_size": 0},
            {"listing_details_threshold": 0},
            {"max_compress_size": -1},
            {"compression_cache_size": -1},
            {"compression_workers": -1},
            {"compression_queue": -1},
            {"compression_queue": 1},
            {"compression_delay": -1},
            {"compress": True},
            {"digest_cache_size": -1},
            {"digest_workers": -1},
            {"digest_queue": -1},
            {"digest_queue": 1},
            {"digest_delay": -1},
            {"access_log_format": "xml"},
            {"access_log_queue": -1},
            {"access_log_overflow": "grow"},
            {"access_log_batch_size": 0},
            {"access_log_batch_wait": -1},
            {"access_log_delay": -1},
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Policy(**values)
        with self.assertRaises(ValueError):
            Policy(cache_control="public\r\nInjected: yes")
        with self.assertRaises(ValueError):
            Policy(cache_control="snowman \N{SNOWMAN}")

    def test_access_log_records_response_facts_and_drains_on_shutdown(self) -> None:
        log_path = self.root / "selector-access.jsonl"

        async def exercise() -> Stats:
            policy = Policy(
                access_log=str(log_path),
                access_log_format="json",
                access_log_queue=2,
                access_log_overflow="wait",
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                found = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\n"
                    b"Referer: https://example.test/\r\nUser-Agent: selector-test\r\n"
                    b"Connection: close\r\n\r\n",
                )
                head = await _exchange(
                    host,
                    port,
                    b"HEAD /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                missing = await _exchange(
                    host,
                    port,
                    b"GET /missing HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                self.assertTrue(found.startswith(b"HTTP/1.1 200"))
                self.assertTrue(head.startswith(b"HTTP/1.1 200"))
                self.assertTrue(missing.startswith(b"HTTP/1.1 404"))
            return stats

        stats = asyncio.run(exercise())
        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        self.assertEqual(stats.access_log_submitted, 3)
        self.assertEqual(stats.access_log_dropped, 0)
        self.assertEqual(stats.access_log_errors, 0)
        self.assertEqual([record["method"] for record in records], ["GET", "HEAD", "GET"])
        self.assertEqual([record["status"] for record in records], [200, 200, 404])
        self.assertEqual([record["size"] for record in records], ["7", "7", "0"])
        self.assertEqual(records[0]["referer"], "https://example.test/")
        self.assertEqual(records[0]["user_agent"], "selector-test")

    def test_access_log_drop_policy_preserves_responses_and_counts_omissions(self) -> None:
        log_path = self.root / "selector-access.log"

        async def exercise() -> Stats:
            policy = Policy(
                access_log=str(log_path),
                access_log_queue=0,
                access_log_overflow="drop",
                access_log_delay=0.1,
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                responses = await asyncio.gather(
                    *(
                        _exchange(
                            host,
                            port,
                            b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                        )
                        for _ in range(6)
                    )
                )
                self.assertTrue(all(response.startswith(b"HTTP/1.1 200") for response in responses))
            return stats

        stats = asyncio.run(exercise())
        self.assertEqual(stats.access_log_submitted, 1)
        self.assertEqual(stats.access_log_dropped, 5)
        self.assertEqual(len(log_path.read_text().splitlines()), 1)

    def test_access_log_wait_policy_backpressures_without_blocking_event_loop(self) -> None:
        log_path = self.root / "selector-access.log"

        async def exercise() -> tuple[Stats, int]:
            policy = Policy(
                access_log=str(log_path),
                access_log_queue=0,
                access_log_overflow="wait",
                access_log_delay=0.05,
            )
            ticks = 0
            running = True

            async def ticker() -> None:
                nonlocal ticks
                while running:
                    ticks += 1
                    await asyncio.sleep(0.005)

            async with _running(self.root, policy) as (host, port, stats, _stop):
                ticker_task = asyncio.create_task(ticker())
                responses = await asyncio.gather(
                    *(
                        _exchange(
                            host,
                            port,
                            b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                        )
                        for _ in range(4)
                    )
                )
                running = False
                await ticker_task
                self.assertTrue(all(response.startswith(b"HTTP/1.1 200") for response in responses))
            return stats, ticks

        stats, ticks = asyncio.run(exercise())
        self.assertEqual(stats.access_log_submitted, 4)
        self.assertEqual(stats.access_log_dropped, 0)
        self.assertGreater(ticks, 10)
        self.assertEqual(len(log_path.read_text().splitlines()), 4)

    def test_access_log_worker_failure_is_counted_without_losing_response(self) -> None:
        log_path = self.root / "selector-access.log"

        async def exercise() -> Stats:
            policy = Policy(access_log=str(log_path), access_log_overflow="wait")
            async with _running(self.root, policy) as (host, port, stats, _stop):
                response = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                self.assertTrue(response.startswith(b"HTTP/1.1 200"))
            return stats

        with mock.patch.object(selector_prototype, "_write_access_batch", side_effect=OSError):
            stats = asyncio.run(exercise())
        self.assertEqual(stats.access_log_submitted, 1)
        self.assertEqual(stats.access_log_errors, 1)

    def test_repr_digest_full_range_head_cache_and_coding_boundary(self) -> None:
        identity = bytes(range(256)) * 100
        page = b"compressible digest page\n" * 2000
        (self.root / "identity.bin").write_bytes(identity)
        (self.root / "page.txt").write_bytes(page)

        async def exercise() -> None:
            policy = Policy(
                digest_workers=1,
                digest_queue=8,
                digest_cache_size=8,
                compress=True,
                compression_workers=1,
                compression_queue=8,
            )
            suffix = b" HTTP/1.1\r\nHost: x\r\nWant-Repr-Digest: sha-256\r\n"
            async with _running(self.root, policy) as (host, port, stats, _stop):
                full = await _exchange(
                    host,
                    port,
                    b"GET /identity.bin" + suffix + b"Connection: close\r\n\r\n",
                )
                ranged = await _exchange(
                    host,
                    port,
                    b"GET /identity.bin"
                    + suffix
                    + b"Range: bytes=3-9\r\nConnection: close\r\n\r\n",
                )
                head = await _exchange(
                    host,
                    port,
                    b"HEAD /identity.bin" + suffix + b"Connection: close\r\n\r\n",
                )
                coded = await _exchange(
                    host,
                    port,
                    b"GET /page.txt"
                    + suffix
                    + b"Accept-Encoding: gzip\r\nConnection: close\r\n\r\n",
                )

            expected = _digest.field_value("sha-256", identity).encode()
            for response in (full, ranged, head):
                self.assertEqual(_response_header(response, b"repr-digest"), expected)
            self.assertEqual(full.split(b"\r\n\r\n", 1)[1], identity)
            self.assertEqual(ranged.split(b"\r\n\r\n", 1)[1], identity[3:10])
            self.assertEqual(head.split(b"\r\n\r\n", 1)[1], b"")
            self.assertIsNone(_response_header(coded, b"repr-digest"))
            self.assertEqual(stats.digest_submitted, 1)
            self.assertEqual(stats.digest_hits, 2)

        asyncio.run(exercise())

    def test_same_identity_digest_miss_is_shared_without_queue_capacity(self) -> None:
        data = b"same digest identity\n" * 4000
        (self.root / "digest.bin").write_bytes(data)

        async def exercise() -> None:
            policy = Policy(
                small_file_buffer_size=0,
                digest_workers=1,
                digest_queue=0,
                digest_delay=0.1,
            )
            request = (
                b"GET /digest.bin HTTP/1.1\r\nHost: x\r\nWant-Repr-Digest: sha-256\r\n"
                b"Connection: close\r\n\r\n"
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                first, second = await asyncio.gather(
                    _exchange(host, port, request),
                    _exchange(host, port, request),
                )
            expected = _digest.field_value("sha-256", data).encode()
            self.assertEqual(_response_header(first, b"repr-digest"), expected)
            self.assertEqual(_response_header(second, b"repr-digest"), expected)
            self.assertEqual(stats.digest_submitted, 1)
            self.assertEqual(stats.digest_shared, 1)
            self.assertEqual(stats.digest_rejected, 0)

        asyncio.run(exercise())

    def test_distinct_digest_saturation_rejects_and_recovers(self) -> None:
        (self.root / "first.bin").write_bytes(b"first" * 10000)
        (self.root / "second.bin").write_bytes(b"second" * 10000)

        async def exercise() -> None:
            policy = Policy(
                small_file_buffer_size=0,
                digest_workers=1,
                digest_queue=0,
                digest_delay=0.1,
            )
            suffix = (
                b" HTTP/1.1\r\nHost: x\r\nWant-Repr-Digest: sha-256\r\nConnection: close\r\n\r\n"
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                first = asyncio.create_task(_exchange(host, port, b"GET /first.bin" + suffix))
                while stats.digest_submitted == 0:
                    await asyncio.sleep(0.001)
                rejected = await _exchange(host, port, b"GET /second.bin" + suffix)
                self.assertTrue(rejected.startswith(b"HTTP/1.1 503"), rejected)
                self.assertTrue((await first).startswith(b"HTTP/1.1 200"))
                recovered = await _exchange(host, port, b"GET /second.bin" + suffix)
                self.assertTrue(recovered.startswith(b"HTTP/1.1 200"), recovered)
            self.assertEqual(stats.digest_submitted, 2)
            self.assertEqual(stats.digest_rejected, 1)

        asyncio.run(exercise())

    def test_cancelled_digest_owns_descriptor_until_worker_completion(self) -> None:
        (self.root / "large-digest.bin").write_bytes(b"digest" * 20000)

        async def exercise() -> None:
            policy = Policy(
                small_file_buffer_size=0,
                digest_workers=1,
                digest_delay=0.1,
                drain_timeout=0.01,
            )
            async with _running(self.root, policy) as (host, port, stats, stop):
                _reader, writer = await asyncio.open_connection(host, port)
                writer.write(
                    b"GET /large-digest.bin HTTP/1.1\r\nHost: x\r\n"
                    b"Want-Repr-Digest: sha-256\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                while stats.digest_submitted == 0:
                    await asyncio.sleep(0.001)
                stop.set()
                await asyncio.sleep(0.02)
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
            self.assertEqual(stats.cancelled, 1)
            self.assertEqual(stats.digest_cancelled, 1)
            self.assertEqual(stats.digest_late_completed, 1)

        asyncio.run(exercise())

    def test_digest_keeps_opened_identity_across_replacement_and_fails_on_truncation(self) -> None:
        path = self.root / "mutable.bin"
        original = b"original digest identity\n" * 3000
        path.write_bytes(original)

        async def replacement_case() -> None:
            policy = Policy(
                small_file_buffer_size=0,
                digest_workers=1,
                digest_delay=0.1,
            )
            request_bytes = (
                b"GET /mutable.bin HTTP/1.1\r\nHost: x\r\nWant-Repr-Digest: sha-256\r\n"
                b"Connection: close\r\n\r\n"
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                request = asyncio.create_task(_exchange(host, port, request_bytes))
                while stats.digest_submitted == 0:
                    await asyncio.sleep(0.001)
                replacement = self.root / "replacement.tmp"
                replacement.write_bytes(b"replacement")
                replacement.replace(path)
                response = await request
            self.assertEqual(response.split(b"\r\n\r\n", 1)[1], original)
            self.assertEqual(
                _response_header(response, b"repr-digest"),
                _digest.field_value("sha-256", original).encode(),
            )

        asyncio.run(replacement_case())

        path.write_bytes(original)

        async def truncation_case() -> None:
            policy = Policy(
                small_file_buffer_size=0,
                digest_workers=1,
                digest_delay=0.1,
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                request = asyncio.create_task(
                    _exchange(
                        host,
                        port,
                        b"GET /mutable.bin HTTP/1.1\r\nHost: x\r\n"
                        b"Want-Repr-Digest: sha-256\r\nConnection: close\r\n\r\n",
                    )
                )
                while stats.digest_submitted == 0:
                    await asyncio.sleep(0.001)
                os.truncate(path, 2)
                response = await request
            self.assertTrue(response.startswith(b"HTTP/1.1 500"), response)
            self.assertEqual(stats.digest_errors, 1)

        asyncio.run(truncation_case())

    def test_gzip_cache_conditionals_ranges_and_head_share_representation(self) -> None:
        page = b"compressible page\n" * 300
        (self.root / "page.txt").write_bytes(page)

        async def exercise() -> None:
            policy = Policy(
                compress=True,
                compression_workers=1,
                compression_queue=8,
                compression_cache_size=1024 * 1024,
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                compressed = await _exchange(
                    host,
                    port,
                    b"GET /page.txt HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\n"
                    b"Connection: close\r\n\r\n",
                )
                etag = _response_header(compressed, b"etag") or b""
                not_modified = await _exchange(
                    host,
                    port,
                    b"GET /page.txt HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\n"
                    b"If-None-Match: " + etag + b"\r\nConnection: close\r\n\r\n",
                )
                cached_head = await _exchange(
                    host,
                    port,
                    b"HEAD /page.txt HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\n"
                    b"Connection: close\r\n\r\n",
                )
                ranged = await _exchange(
                    host,
                    port,
                    b"GET /page.txt HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\n"
                    b"Range: bytes=0-9\r\nConnection: close\r\n\r\n",
                )
                unsatisfiable = await _exchange(
                    host,
                    port,
                    b"GET /page.txt HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\n"
                    b"Range: bytes=99999-100000\r\nConnection: close\r\n\r\n",
                )

            self.assertTrue(compressed.startswith(b"HTTP/1.1 200"), compressed)
            self.assertEqual(_response_header(compressed, b"content-encoding"), b"gzip")
            self.assertEqual(_response_header(compressed, b"vary"), b"Accept-Encoding")
            self.assertIsNone(_response_header(compressed, b"accept-ranges"))
            self.assertTrue(etag.endswith(b'-gz"'), etag)
            self.assertEqual(gzip.decompress(compressed.split(b"\r\n\r\n", 1)[1]), page)
            self.assertTrue(not_modified.startswith(b"HTTP/1.1 304"), not_modified)
            self.assertEqual(_response_header(not_modified, b"vary"), b"Accept-Encoding")
            self.assertEqual(not_modified.split(b"\r\n\r\n", 1)[1], b"")
            self.assertEqual(cached_head.split(b"\r\n\r\n", 1)[1], b"")
            self.assertTrue(ranged.startswith(b"HTTP/1.1 206"), ranged)
            self.assertIsNone(_response_header(ranged, b"content-encoding"))
            self.assertEqual(ranged.split(b"\r\n\r\n", 1)[1], page[:10])
            self.assertTrue(unsatisfiable.startswith(b"HTTP/1.1 416"), unsatisfiable)
            self.assertEqual(_response_header(unsatisfiable, b"vary"), b"Accept-Encoding")
            self.assertEqual(stats.compression_submitted, 1)
            self.assertEqual(stats.compression_hits, 1)

        asyncio.run(exercise())

    def test_same_key_compression_miss_is_shared_without_consuming_queue(self) -> None:
        page = b"same key\n" * 4000
        (self.root / "page.txt").write_bytes(page)

        async def exercise() -> None:
            policy = Policy(
                compress=True,
                compression_workers=1,
                compression_queue=0,
                compression_cache_size=1024 * 1024,
                compression_delay=0.1,
            )
            request = (
                b"GET /page.txt HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\n"
                b"Connection: close\r\n\r\n"
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                first, second = await asyncio.gather(
                    _exchange(host, port, request),
                    _exchange(host, port, request),
                )
            self.assertEqual(gzip.decompress(first.split(b"\r\n\r\n", 1)[1]), page)
            self.assertEqual(first.split(b"\r\n\r\n", 1)[1], second.split(b"\r\n\r\n", 1)[1])
            self.assertEqual(stats.compression_submitted, 1)
            self.assertEqual(stats.compression_shared, 1)
            self.assertEqual(stats.compression_rejected, 0)

        asyncio.run(exercise())

    def test_distinct_compression_miss_saturation_rejects_and_recovers(self) -> None:
        (self.root / "first.txt").write_bytes(b"first\n" * 6000)
        (self.root / "second.txt").write_bytes(b"second\n" * 6000)

        async def exercise() -> None:
            policy = Policy(
                compress=True,
                compression_workers=1,
                compression_queue=0,
                compression_delay=0.1,
            )
            suffix = b" HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\nConnection: close\r\n\r\n"
            async with _running(self.root, policy) as (host, port, stats, _stop):
                first = asyncio.create_task(_exchange(host, port, b"GET /first.txt" + suffix))
                while stats.compression_submitted == 0:
                    await asyncio.sleep(0.001)
                rejected = await _exchange(host, port, b"GET /second.txt" + suffix)
                self.assertTrue(rejected.startswith(b"HTTP/1.1 503"), rejected)
                self.assertEqual(
                    gzip.decompress((await first).split(b"\r\n\r\n", 1)[1]),
                    b"first\n" * 6000,
                )
                recovered = await _exchange(host, port, b"GET /second.txt" + suffix)
                self.assertTrue(recovered.startswith(b"HTTP/1.1 200"), recovered)
            self.assertEqual(stats.compression_submitted, 2)
            self.assertEqual(stats.compression_rejected, 1)

        asyncio.run(exercise())

    def test_cancelled_large_compression_uses_owned_descriptor_to_completion(self) -> None:
        (self.root / "large.txt").write_bytes(b"large page\n" * 10000)

        async def exercise() -> None:
            policy = Policy(
                compress=True,
                compression_workers=1,
                compression_queue=0,
                compression_delay=0.1,
                drain_timeout=0.01,
            )
            async with _running(self.root, policy) as (host, port, stats, stop):
                _reader, writer = await asyncio.open_connection(host, port)
                writer.write(
                    b"GET /large.txt HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                while stats.compression_submitted == 0:
                    await asyncio.sleep(0.001)
                stop.set()
                await asyncio.sleep(0.02)
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
            self.assertEqual(stats.cancelled, 1)
            self.assertEqual(stats.compression_cancelled, 1)
            self.assertEqual(stats.compression_late_completed, 1)

        asyncio.run(exercise())

    def test_compression_keeps_opened_identity_across_atomic_replacement(self) -> None:
        path = self.root / "replace-compressed.txt"
        original = b"original compressed identity\n" * 2000
        path.write_bytes(original)

        async def exercise() -> None:
            policy = Policy(
                compress=True,
                compression_workers=1,
                compression_queue=0,
                compression_delay=0.1,
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                request = asyncio.create_task(
                    _exchange(
                        host,
                        port,
                        b"GET /replace-compressed.txt HTTP/1.1\r\nHost: x\r\n"
                        b"Accept-Encoding: gzip\r\nConnection: close\r\n\r\n",
                    )
                )
                while stats.compression_submitted == 0:
                    await asyncio.sleep(0.001)
                replacement = self.root / "replacement.tmp"
                replacement.write_bytes(b"replacement")
                replacement.replace(path)
                response = await request
            self.assertTrue(response.startswith(b"HTTP/1.1 200"), response)
            self.assertEqual(gzip.decompress(response.split(b"\r\n\r\n", 1)[1]), original)

        asyncio.run(exercise())

    def test_truncation_during_compression_fails_closed(self) -> None:
        path = self.root / "truncate-compressed.txt"
        path.write_bytes(b"compress then truncate\n" * 2000)

        async def exercise() -> None:
            policy = Policy(
                compress=True,
                compression_workers=1,
                compression_queue=0,
                compression_delay=0.1,
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                request = asyncio.create_task(
                    _exchange(
                        host,
                        port,
                        b"GET /truncate-compressed.txt HTTP/1.1\r\nHost: x\r\n"
                        b"Accept-Encoding: gzip\r\nConnection: close\r\n\r\n",
                    )
                )
                while stats.compression_submitted == 0:
                    await asyncio.sleep(0.001)
                os.truncate(path, 2)
                response = await request
            self.assertTrue(response.startswith(b"HTTP/1.1 500"), response)
            self.assertEqual(stats.compression_errors, 1)

        asyncio.run(exercise())

    def test_directory_redirect_index_and_explicit_listing_gap(self) -> None:
        indexed = self.root / "indexed"
        indexed.mkdir()
        (indexed / "index.html").write_bytes(b"index payload")
        (self.root / "listing").mkdir()

        async def exercise() -> None:
            async with _running(self.root, Policy(cache_control="public, max-age=60")) as (
                host,
                port,
                _stats,
                _stop,
            ):
                redirect = await _exchange(
                    host,
                    port,
                    b"GET /indexed?view=1 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                indexed_get = await _exchange(
                    host,
                    port,
                    b"GET /indexed/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                indexed_head = await _exchange(
                    host,
                    port,
                    b"HEAD /indexed/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                listing = await _exchange(
                    host,
                    port,
                    b"GET /listing/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )

            self.assertTrue(redirect.startswith(b"HTTP/1.1 301"), redirect)
            self.assertEqual(_response_header(redirect, b"location"), b"/indexed/?view=1")
            self.assertTrue(indexed_get.startswith(b"HTTP/1.1 200"), indexed_get)
            self.assertEqual(indexed_get.split(b"\r\n\r\n", 1)[1], b"index payload")
            self.assertEqual(_response_header(indexed_get, b"cache-control"), b"public, max-age=60")
            self.assertEqual(_response_header(indexed_get, b"vary"), b"Accept-Encoding")
            self.assertEqual(indexed_head.split(b"\r\n\r\n", 1)[1], b"")
            self.assertTrue(listing.startswith(b"HTTP/1.1 501"), listing)

        asyncio.run(exercise())

    def test_bounded_listing_matches_query_theme_head_security_and_compression(self) -> None:
        directory = self.root / "listing"
        directory.mkdir()
        (directory / "alpha.txt").write_bytes(b"a")
        (directory / "beta.txt").write_bytes(b"bb")
        (directory / "other.py").write_bytes(b"ccc")
        (directory / ".hidden.txt").write_bytes(b"hidden")
        fixed_mtime = 1_700_000_000
        for path in directory.iterdir():
            os.utime(path, (fixed_mtime, fixed_mtime))

        async def exercise() -> None:
            policy = Policy(
                listing_workers=1,
                listing_queue=8,
                listing_page_size=2,
                compress=True,
                compression_workers=1,
                compression_queue=1,
            )
            target = "/listing/?C=S&O=D&q=.txt&theme=dark"
            request_base = (
                f"{{method}} {target} HTTP/1.1\r\nHost: x\r\n"
                "Cookie: servery_theme=light\r\n{extra}Connection: close\r\n\r\n"
            )
            async with _running(self.root, policy) as (host, port, stats, _stop):
                plain = await _exchange(
                    host,
                    port,
                    request_base.format(method="GET", extra="").encode(),
                )
                head = await _exchange(
                    host,
                    port,
                    request_base.format(method="HEAD", extra="").encode(),
                )
                coded = await _exchange(
                    host,
                    port,
                    request_base.format(method="GET", extra="Accept-Encoding: gzip\r\n").encode(),
                )

            options = listing.request_options(target, "servery_theme=light")
            expected = listing.render(
                str(directory),
                options.display,
                show_hidden=False,
                sort=options.sort,
                order=options.order,
                query=options.query,
                ext=options.ext,
                page=options.page,
                per_page=2,
                theme=options.theme,
                max_entries=100_000,
                details_threshold=10_000,
            )
            self.assertEqual(plain.split(b"\r\n\r\n", 1)[1], expected)
            self.assertEqual(head.split(b"\r\n\r\n", 1)[1], b"")
            self.assertEqual(_response_header(head, b"content-length"), str(len(expected)).encode())
            self.assertEqual(gzip.decompress(coded.split(b"\r\n\r\n", 1)[1]), expected)
            self.assertEqual(_response_header(coded, b"content-encoding"), b"gzip")
            self.assertEqual(_response_header(plain, b"vary"), b"Accept-Encoding")
            self.assertEqual(
                _response_header(plain, b"content-security-policy"),
                _static.GENERATED_CSP.encode(),
            )
            self.assertEqual(_response_header(plain, b"referrer-policy"), b"no-referrer")
            self.assertIn(b"servery_theme=dark", _response_header(plain, b"set-cookie") or b"")
            self.assertNotIn(b".hidden.txt", expected)
            self.assertEqual(stats.listing_submitted, 3)
            self.assertEqual(stats.listing_errors, 0)

        asyncio.run(exercise())

    def test_listing_saturation_rejects_and_recovers(self) -> None:
        directory = self.root / "listing"
        directory.mkdir()
        (directory / "entry.txt").write_bytes(b"entry")

        async def exercise() -> None:
            policy = Policy(listing_workers=1, listing_queue=0, listing_delay=0.1)
            request = b"GET /listing/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            async with _running(self.root, policy) as (host, port, stats, _stop):
                first = asyncio.create_task(_exchange(host, port, request))
                while stats.listing_submitted == 0:
                    await asyncio.sleep(0.001)
                rejected = await _exchange(host, port, request)
                self.assertTrue(rejected.startswith(b"HTTP/1.1 503"), rejected)
                self.assertTrue((await first).startswith(b"HTTP/1.1 200"))
                recovered = await _exchange(host, port, request)
                self.assertTrue(recovered.startswith(b"HTTP/1.1 200"), recovered)
            self.assertEqual(stats.listing_submitted, 2)
            self.assertEqual(stats.listing_rejected, 1)

        asyncio.run(exercise())

    def test_cancelled_listing_finishes_under_planner_ownership(self) -> None:
        (self.root / "listing").mkdir()

        async def exercise() -> None:
            policy = Policy(
                listing_workers=1,
                listing_delay=0.1,
                drain_timeout=0.01,
            )
            async with _running(self.root, policy) as (host, port, stats, stop):
                _reader, writer = await asyncio.open_connection(host, port)
                writer.write(b"GET /listing/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                await writer.drain()
                while stats.listing_submitted == 0:
                    await asyncio.sleep(0.001)
                stop.set()
                await asyncio.sleep(0.02)
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
            self.assertEqual(stats.cancelled, 1)
            self.assertEqual(stats.listing_cancelled, 1)
            self.assertEqual(stats.listing_late_completed, 1)

        asyncio.run(exercise())

    def test_listing_shutdown_cancels_queued_jobs_but_waits_for_running_job(self) -> None:
        (self.root / "listing").mkdir()

        async def exercise() -> None:
            policy = Policy(
                listing_workers=1,
                listing_queue=2,
                listing_delay=0.1,
                drain_timeout=0.01,
            )
            writers: list[asyncio.StreamWriter] = []
            async with _running(self.root, policy) as (host, port, stats, stop):
                for _ in range(3):
                    _reader, writer = await asyncio.open_connection(host, port)
                    writers.append(writer)
                    writer.write(b"GET /listing/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                while stats.listing_submitted < 3:
                    await asyncio.sleep(0.001)
                stop.set()
                await asyncio.sleep(0.02)
                for writer in writers:
                    writer.close()
                    with contextlib.suppress(ConnectionError):
                        await writer.wait_closed()
            self.assertEqual(stats.listing_cancelled, 3)
            self.assertEqual(stats.listing_shutdown_cancelled, 2)
            self.assertEqual(stats.listing_late_completed, 1)

        asyncio.run(exercise())

    def test_listing_filesystem_failure_is_404_and_worker_failure_is_500(self) -> None:
        (self.root / "listing").mkdir()
        request = b"GET /listing/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"

        async def exercise(error: BaseException) -> tuple[bytes, Stats]:
            with mock.patch.object(selector_prototype, "_render_listing", side_effect=error):
                async with _running(self.root, Policy(listing_workers=1)) as (
                    host,
                    port,
                    stats,
                    _stop,
                ):
                    response = await _exchange(host, port, request)
            return response, stats

        missing, missing_stats = asyncio.run(exercise(OSError("scan failed")))
        broken, broken_stats = asyncio.run(exercise(RuntimeError("render failed")))
        self.assertTrue(missing.startswith(b"HTTP/1.1 404"), missing)
        self.assertTrue(broken.startswith(b"HTTP/1.1 500"), broken)
        self.assertEqual(missing_stats.listing_errors, 1)
        self.assertEqual(broken_stats.listing_errors, 1)

    def test_download_disposition_and_configurable_spa_fallback(self) -> None:
        (self.root / "index.html").write_bytes(b"spa payload")

        async def exercise() -> None:
            async with _running(self.root, Policy()) as (host, port, _stats, _stop):
                disabled = await _exchange(
                    host,
                    port,
                    b"GET /client/route HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
            async with _running(self.root, Policy(spa=True)) as (host, port, _stats, _stop):
                fallback = await _exchange(
                    host,
                    port,
                    b"GET /client/route HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                fallback_download = await _exchange(
                    host,
                    port,
                    b"GET /client/route?download=1 HTTP/1.1\r\nHost: x\r\n"
                    b"Connection: close\r\n\r\n",
                )
                file_download = await _exchange(
                    host,
                    port,
                    b"GET /small.txt?download=1 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                fallback_head = await _exchange(
                    host,
                    port,
                    b"HEAD /client/route HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )

            self.assertTrue(disabled.startswith(b"HTTP/1.1 404"), disabled)
            self.assertTrue(fallback.startswith(b"HTTP/1.1 200"), fallback)
            self.assertEqual(fallback.split(b"\r\n\r\n", 1)[1], b"spa payload")
            self.assertEqual(fallback_head.split(b"\r\n\r\n", 1)[1], b"")
            self.assertIn(
                b'filename="index.html"',
                _response_header(fallback_download, b"content-disposition") or b"",
            )
            self.assertIn(
                b'filename="small.txt"',
                _response_header(file_download, b"content-disposition") or b"",
            )

        asyncio.run(exercise())

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_spa_fallback_rejects_index_symlink_escape(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside-spa.html"
        outside.write_bytes(b"TOP SECRET")
        try:
            try:
                (self.root / "index.html").symlink_to(outside)
            except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
                self.skipTest("symlink creation not permitted")

            async def exercise() -> None:
                async with _running(self.root, Policy(spa=True)) as (host, port, _stats, _stop):
                    response = await _exchange(
                        host,
                        port,
                        b"GET /client/route HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                    )
                self.assertTrue(response.startswith(b"HTTP/1.1 404"), response)
                self.assertNotIn(b"TOP SECRET", response)

            asyncio.run(exercise())
        finally:
            outside.unlink(missing_ok=True)

    def test_pipelined_heads_reuse_owned_remainder(self) -> None:
        async def exercise() -> None:
            async with _running(self.root, Policy()) as (host, port, stats, _stop):
                response = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\n\r\n"
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
            self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 2)
            self.assertEqual(response.count(b"payload"), 2)
            self.assertEqual(stats.completed, 2)

        asyncio.run(exercise())

    def test_bounded_filesystem_executor_rejects_and_recovers(self) -> None:
        async def exercise() -> None:
            policy = Policy(filesystem_workers=1, filesystem_delay=0.1)
            async with _running(self.root, policy) as (host, port, stats, _stop):
                first = asyncio.create_task(
                    _exchange(
                        host,
                        port,
                        b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                    )
                )
                await asyncio.sleep(0.02)
                rejected = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                self.assertTrue(rejected.startswith(b"HTTP/1.1 503"), rejected)
                self.assertIn(b"payload", await first)
                recovered = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                self.assertIn(b"payload", recovered)
            self.assertEqual(stats.filesystem_submitted, 2)
            self.assertEqual(stats.filesystem_rejected, 1)
            self.assertEqual(stats.completed, 2)

        asyncio.run(exercise())

    def test_cancelled_filesystem_result_is_closed_when_worker_finishes(self) -> None:
        async def exercise() -> None:
            policy = Policy(
                filesystem_workers=1,
                filesystem_delay=0.1,
                drain_timeout=0.01,
            )
            async with _running(self.root, policy) as (host, port, stats, stop):
                _reader, writer = await asyncio.open_connection(host, port)
                writer.write(b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                await writer.drain()
                await asyncio.sleep(0.02)
                stop.set()
                await asyncio.sleep(0.02)
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
            self.assertEqual(stats.cancelled, 1)
            self.assertEqual(stats.filesystem_cancelled, 1)
            self.assertEqual(stats.filesystem_late_closed, 1)

        asyncio.run(exercise())

    def test_request_limit_closes_before_later_pipeline(self) -> None:
        async def exercise() -> None:
            async with _running(self.root, Policy(max_requests=1)) as (host, port, stats, _stop):
                response = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\n\r\n"
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
            self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 1)
            self.assertIn(b"Connection: close", response)
            self.assertEqual(stats.completed, 1)

        asyncio.run(exercise())

    def test_missing_file_preserves_keepalive_for_later_pipeline(self) -> None:
        async def exercise() -> None:
            async with _running(self.root, Policy()) as (host, port, stats, _stop):
                response = await _exchange(
                    host,
                    port,
                    b"GET /missing HTTP/1.1\r\nHost: x\r\n\r\n"
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
            self.assertEqual(response.count(b"HTTP/1.1 404 Not Found"), 1)
            self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 1)
            self.assertIn(b"payload", response)
            self.assertEqual(stats.completed, 2)

        asyncio.run(exercise())

    def test_ranges_conditionals_if_range_and_head_share_selection(self) -> None:
        async def exercise() -> None:
            async with _running(self.root, Policy()) as (host, port, _stats, _stop):
                initial = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                etag = _response_header(initial, b"etag")
                self.assertIsNotNone(etag)
                not_modified = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nIf-None-Match: "
                    + (etag or b"")
                    + b"\r\nConnection: close\r\n\r\n",
                )
                partial = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nRange: bytes=1-3\r\n"
                    b"Connection: close\r\n\r\n",
                )
                matching = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nRange: bytes=1-3\r\n"
                    b"If-Range: " + (etag or b"") + b"\r\nConnection: close\r\n\r\n",
                )
                changed = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nRange: bytes=1-3\r\n"
                    b'If-Range: "changed"\r\nConnection: close\r\n\r\n',
                )
                unsatisfiable = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nRange: bytes=99-100\r\n"
                    b"Connection: close\r\n\r\n",
                )
                head_range = await _exchange(
                    host,
                    port,
                    b"HEAD /small.txt HTTP/1.1\r\nHost: x\r\nRange: bytes=1-3\r\n"
                    b"Connection: close\r\n\r\n",
                )

            self.assertTrue(not_modified.startswith(b"HTTP/1.1 304"), not_modified)
            self.assertEqual(not_modified.split(b"\r\n\r\n", 1)[1], b"")
            for response in (partial, matching, head_range):
                self.assertTrue(response.startswith(b"HTTP/1.1 206"), response)
                self.assertEqual(_response_header(response, b"content-range"), b"bytes 1-3/7")
            self.assertEqual(partial.split(b"\r\n\r\n", 1)[1], b"ayl")
            self.assertEqual(matching.split(b"\r\n\r\n", 1)[1], b"ayl")
            self.assertEqual(head_range.split(b"\r\n\r\n", 1)[1], b"")
            self.assertTrue(changed.startswith(b"HTTP/1.1 200"), changed)
            self.assertEqual(changed.split(b"\r\n\r\n", 1)[1], b"payload")
            self.assertTrue(unsatisfiable.startswith(b"HTTP/1.1 416"), unsatisfiable)
            self.assertEqual(
                _response_header(unsatisfiable, b"content-range"),
                b"bytes */7",
            )

        asyncio.run(exercise())

    def test_opened_identity_survives_atomic_path_replacement(self) -> None:
        path = self.root / "replace.txt"
        path.write_bytes(b"original")
        prepared = _prepare_file(str(path), "GET", 0, 0)
        replacement = self.root / "replacement.tmp"
        replacement.write_bytes(b"new")
        replacement.replace(path)
        try:
            self.assertEqual(prepared.stat.st_size, 8)
            self.assertEqual(prepared.handle.read(), b"original")
        finally:
            prepared.close()

    def test_truncation_after_open_aborts_incomplete_sendfile(self) -> None:
        path = self.root / "truncate.txt"
        path.write_bytes(b"1234567")
        original_prepare = selector_prototype._prepare_file

        def prepare_then_truncate(*args: Any, **kwargs: Any):
            prepared = original_prepare(*args, **kwargs)
            os.truncate(path, 2)
            return prepared

        async def exercise() -> None:
            policy = Policy(small_file_buffer_size=0)
            with mock.patch.object(
                selector_prototype,
                "_prepare_file",
                side_effect=prepare_then_truncate,
            ):
                async with _running(self.root, policy) as (host, port, stats, _stop):
                    response = await _exchange(
                        host,
                        port,
                        b"GET /truncate.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                    )
            self.assertEqual(_response_header(response, b"content-length"), b"7")
            self.assertEqual(response.split(b"\r\n\r\n", 1)[1], b"12")
            self.assertEqual(stats.transfer_errors, 1)

        asyncio.run(exercise())

    def test_declared_body_forces_close_without_reparsing_body_bytes(self) -> None:
        async def exercise() -> None:
            async with _running(self.root, Policy()) as (host, port, stats, _stop):
                response = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\n"
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
            self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 1)
            self.assertEqual(stats.completed, 1)

        asyncio.run(exercise())

    def test_shared_parser_error_is_serialized(self) -> None:
        async def exercise() -> None:
            async with _running(self.root, Policy()) as (host, port, stats, _stop):
                response = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: one\r\nHost: two\r\n\r\n",
                )
            self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request"), response)
            self.assertEqual(stats.parse_errors, 1)

        asyncio.run(exercise())

    def test_total_request_head_timeout_aborts_slowloris(self) -> None:
        async def exercise() -> None:
            async with _running(self.root, Policy(active_timeout=0.05)) as (
                host,
                port,
                stats,
                _stop,
            ):
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(b"GET /small.txt HTTP/1.1\r\n")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
                writer.close()
                await writer.wait_closed()
            self.assertEqual(stats.head_timeouts, 1)

        asyncio.run(exercise())

    def test_admission_rejects_and_recovers_without_queue(self) -> None:
        async def exercise() -> None:
            async with _running(self.root, Policy(max_connections=1)) as (
                host,
                port,
                stats,
                _stop,
            ):
                _held_reader, held_writer = await asyncio.open_connection(host, port)
                held_writer.write(b"GET /small.txt HTTP/1.1\r\n")
                await held_writer.drain()
                await asyncio.sleep(0.02)
                rejected_reader, rejected_writer = await asyncio.open_connection(host, port)
                self.assertEqual(await asyncio.wait_for(rejected_reader.read(), 1), b"")
                rejected_writer.close()
                await rejected_writer.wait_closed()
                held_writer.close()
                await held_writer.wait_closed()
                await asyncio.sleep(0.02)
                response = await _exchange(
                    host,
                    port,
                    b"GET /small.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                self.assertIn(b"payload", response)
            self.assertEqual(stats.rejected, 1)
            self.assertEqual(stats.completed, 1)

        asyncio.run(exercise())

    def test_graceful_stop_cancels_after_drain_deadline(self) -> None:
        async def exercise() -> None:
            policy = Policy(active_timeout=30, drain_timeout=0.05)
            async with _running(self.root, policy) as (host, port, stats, stop):
                _reader, writer = await asyncio.open_connection(host, port)
                writer.write(b"GET /small.txt HTTP/1.1\r\n")
                await writer.drain()
                await asyncio.sleep(0.02)
                stop.set()
                await asyncio.sleep(0.1)
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
            self.assertEqual(stats.cancelled, 1, stats)

        asyncio.run(exercise())

    def test_write_timeout_releases_nonreading_large_transfer(self) -> None:
        (self.root / "large.bin").write_bytes(b"x" * (32 * 1024 * 1024))

        async def exercise() -> None:
            policy = Policy(write_timeout=0.05)
            async with _running(self.root, policy) as (host, port, stats, _stop):
                _reader, writer = await asyncio.open_connection(host, port)
                raw_socket = writer.get_extra_info("socket")
                raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                writer.write(b"GET /large.bin HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                await writer.drain()
                deadline = asyncio.get_running_loop().time() + 2
                while stats.write_timeouts == 0 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
            self.assertEqual(stats.write_timeouts, 1)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
