"""On-the-fly gzip content-coding (RFC 9110 §8.4.1.3 / §12.5.3) tests."""

from __future__ import annotations

import gzip
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from servery import _compress, _response
from servery.config import Config
from tests._harness import raw_exchange, serving


class NegotiationTest(unittest.TestCase):
    def test_accepts_gzip(self):
        for header in ("gzip", "gzip, deflate", "*", "deflate, gzip;q=0.8", "GZIP", "x-gzip"):
            self.assertTrue(_compress.accepts_gzip(header), header)

    def test_rejects_gzip(self):
        for header in (
            "",
            "identity",
            "deflate",
            "br",
            "gzip;q=0",
            "gzip;q=0.0",
            "gzip;q=notanumber",  # unparseable q-value -> treated as q=0
            "*;q=0",
            "identity, *;q=0",
        ):
            self.assertFalse(_compress.accepts_gzip(header), header)

    def test_compressible(self):
        for ctype in (
            "text/html; charset=utf-8",
            "text/plain",
            "application/json",
            "application/javascript",
            "image/svg+xml",
            "application/manifest+json",
            "font/ttf",
        ):
            self.assertTrue(_compress.compressible(ctype), ctype)

    def test_not_compressible(self):
        for ctype in (
            "image/jpeg",
            "image/png",
            "video/mp4",
            "application/zip",
            "application/gzip",
            "font/woff2",
            "application/octet-stream",
        ):
            self.assertFalse(_compress.compressible(ctype), ctype)

    def test_gzip_roundtrips(self):
        data = b"servery " * 500
        self.assertEqual(gzip.decompress(_compress.gzip_bytes(data)), data)

    def test_accepts_zstd(self):
        for header in ("zstd", "gzip, zstd", "*", "zstd;q=0.5", "ZSTD"):
            self.assertTrue(_compress.accepts_zstd(header), header)
        for header in ("", "gzip", "zstd;q=0", "*;q=0"):
            self.assertFalse(_compress.accepts_zstd(header), header)

    def test_negotiate_prefers_zstd_when_available(self):
        if _compress.HAVE_ZSTD:
            self.assertEqual(_compress.negotiate("gzip, zstd", enabled=True), "zstd")
        # gzip-only always yields gzip, regardless of zstd support.
        self.assertEqual(_compress.negotiate("gzip", enabled=True), "gzip")
        self.assertIsNone(_compress.negotiate("identity", enabled=True))
        self.assertIsNone(_compress.negotiate("gzip", enabled=False))

    def test_negotiate_falls_back_to_gzip_without_zstd(self):
        # Simulate a 3.13 interpreter (no compression.zstd): zstd must not be offered.
        original = _compress.HAVE_ZSTD
        _compress.HAVE_ZSTD = False
        try:
            self.assertEqual(_compress.negotiate("gzip, zstd", enabled=True), "gzip")
        finally:
            _compress.HAVE_ZSTD = original

    @unittest.skipUnless(_compress.HAVE_ZSTD, "zstd needs Python 3.14+ (compression.zstd)")
    def test_zstd_roundtrips(self):
        from compression import zstd  # ty: ignore[unresolved-import]

        data = b"servery " * 500
        self.assertEqual(zstd.decompress(_compress.zstd_bytes(data)), data)


class CompressionCacheTest(unittest.TestCase):
    @staticmethod
    def _key(name: str) -> _compress.CacheKey:
        return (name, 1, 1, "gzip", _compress.GZIP_LEVEL)

    def test_byte_budget_and_lru_eviction(self):
        cache = _compress.CompressionCache(5)
        first, second, third = (self._key(name) for name in ("a", "b", "c"))
        cache.put(first, b"aaa")
        cache.put(second, b"bb")
        self.assertEqual(cache.current_bytes, 5)
        self.assertEqual(cache.get(first), b"aaa")  # promote first over second
        cache.put(third, b"cc")
        self.assertIsNone(cache.get(second))
        self.assertEqual(cache.get(first), b"aaa")
        self.assertEqual(cache.get(third), b"cc")
        self.assertEqual(cache.current_bytes, 5)

    def test_disabled_cache_has_no_retained_bytes(self):
        cache = _compress.CompressionCache(0)
        cache.put(self._key("a"), b"value")
        self.assertEqual(cache.current_bytes, 0)
        self.assertIsNone(cache.get(self._key("a")))
        self.assertEqual(cache.get_or_compute(self._key("a"), lambda: b"fresh"), b"fresh")

    def test_replacement_and_oversized_values_honor_byte_budget(self):
        cache = _compress.CompressionCache(3)
        key = self._key("a")
        cache.put(key, b"a")
        cache.put(key, b"bb")
        self.assertEqual(cache.current_bytes, 2)
        self.assertEqual(cache.get(key), b"bb")
        cache.put(self._key("too-large"), b"four")
        self.assertEqual(cache.current_bytes, 2)

    def test_cache_key_respects_explicit_level(self):
        with tempfile.NamedTemporaryFile() as handle:
            stat = Path(handle.name).stat()
            self.assertEqual(_compress.cache_key(handle.name, stat, "gzip", 1)[-1], 1)

    def test_concurrent_miss_is_computed_once(self):
        cache = _compress.CompressionCache(1024)
        key = self._key("hot")
        barrier = threading.Barrier(5)
        calls = 0
        calls_lock = threading.Lock()
        results: list[bytes] = []

        def factory() -> bytes:
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return b"encoded"

        def worker() -> None:
            barrier.wait()
            results.append(cache.get_or_compute(key, factory))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        self.assertEqual(calls, 1)
        self.assertEqual(results, [b"encoded"] * 4)

    def test_distinct_cache_keys_compute_concurrently(self):
        cache = _compress.CompressionCache(1024)
        rendezvous = threading.Barrier(2, timeout=1)
        results: list[bytes] = []
        errors: list[BaseException] = []

        def worker(name: str) -> None:
            try:
                result = cache.get_or_compute(
                    self._key(name),
                    lambda: (rendezvous.wait(), name.encode())[1],
                )
                results.append(result)
            except BaseException as exc:  # capture thread failures for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

        self.assertEqual(errors, [])
        self.assertCountEqual(results, [b"a", b"b"])
        self.assertEqual(cache._flights, {})  # lifecycle invariant

    def test_disabled_cache_shares_only_concurrent_same_key_result(self):
        cache = _compress.CompressionCache(0)
        key = self._key("uncached-hot")
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        results: list[bytes] = []

        def factory() -> bytes:
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(1)
            return b"transient"

        first = threading.Thread(target=lambda: results.append(cache.get_or_compute(key, factory)))
        second = threading.Thread(target=lambda: results.append(cache.get_or_compute(key, factory)))
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        time.sleep(0.01)
        release.set()
        first.join(2)
        second.join(2)

        self.assertEqual(calls, 1)
        self.assertEqual(results, [b"transient", b"transient"])
        self.assertEqual(cache.current_bytes, 0)
        self.assertIsNone(cache.get(key))
        self.assertEqual(cache.get_or_compute(key, lambda: b"later"), b"later")

    def test_single_flight_failure_reaches_waiters_and_is_reclaimed(self):
        cache = _compress.CompressionCache(1024)
        key = self._key("broken")
        entered = threading.Event()
        release = threading.Event()
        errors: list[str] = []
        calls = 0

        def factory() -> bytes:
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(1)
            raise OSError("encode failed")

        def worker() -> None:
            try:
                cache.get_or_compute(key, factory)
            except OSError as exc:
                errors.append(str(exc))

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        time.sleep(0.01)
        release.set()
        first.join(2)
        second.join(2)

        self.assertEqual(calls, 1)
        self.assertEqual(errors, ["encode failed", "encode failed"])
        self.assertEqual(cache._flights, {})

    def test_shared_response_builder_reuses_and_invalidates_encoded_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "hot.txt")
            path.write_bytes(b"x" * 4000)
            config = Config.create(tmp, quiet=True, compression_cache_size=1024 * 1024)
            cache = _compress.CompressionCache(config.compression_cache_size)
            with mock.patch.object(_compress, "encode", wraps=_compress.encode) as encode:
                first = _response.build_static(
                    config, str(path), "/hot.txt", "gzip", tls=False, compression_cache=cache
                )
                second = _response.build_static(
                    config, str(path), "/hot.txt", "gzip", tls=False, compression_cache=cache
                )
                self.assertEqual(encode.call_count, 1)
                self.assertEqual(first[2], second[2])
                path.write_bytes(b"y" * 5000)
                _response.build_static(
                    config, str(path), "/hot.txt", "gzip", tls=False, compression_cache=cache
                )
                self.assertEqual(encode.call_count, 2)

    @unittest.skipIf(_compress.HAVE_ZSTD, "unavailable branch is specific to Python 3.13")
    def test_zstd_encoder_fails_explicitly_when_unavailable(self):
        with self.assertRaises(RuntimeError):
            _compress.zstd_bytes(b"data")


class _ServerCase(unittest.TestCase):
    compress = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # write_bytes (not write_text) so the content is identical on every OS —
        # Windows text mode would translate "\n" to "\r\n" and break the exact-body
        # assertions below.
        (root / "page.html").write_bytes(b"<h1>hi</h1>\n" + b"x" * 4000)  # compressible, > 1 KiB
        (root / "tiny.txt").write_text("small")  # below the 1 KiB threshold
        (root / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"j" * 4000)  # not compressible
        self.cfg = Config.create(
            str(root), host="127.0.0.1", port=0, quiet=True, compress=self.compress
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _get(self, path, *, accept_encoding=None, extra=b""):
        head = f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n".encode()
        if accept_encoding is not None:
            head += f"Accept-Encoding: {accept_encoding}\r\n".encode()
        with serving(self.cfg) as (host, port):
            return raw_exchange(host, port, head + extra + b"\r\n")


class GzipServerTest(_ServerCase):
    def _split(self, resp):
        head, _, body = resp.partition(b"\r\n\r\n")
        return head.lower(), body

    def test_compresses_text_when_accepted(self):
        head, body = self._split(self._get("/page.html", accept_encoding="gzip"))
        self.assertIn(b"content-encoding: gzip", head)
        self.assertIn(b"vary: accept-encoding", head)
        self.assertNotIn(b"accept-ranges", head)  # a gzip body is not byte-rangeable
        self.assertIn(b"-gz", head)  # distinct ETag for the encoded representation
        self.assertEqual(gzip.decompress(body), b"<h1>hi</h1>\n" + b"x" * 4000)

    def test_identity_when_not_accepted_but_still_varies(self):
        head, body = self._split(self._get("/page.html"))  # no Accept-Encoding header
        self.assertNotIn(b"content-encoding", head)
        self.assertIn(b"vary: accept-encoding", head)  # cache must key on it regardless
        self.assertIn(b"accept-ranges: bytes", head)
        self.assertEqual(body, b"<h1>hi</h1>\n" + b"x" * 4000)

    def test_range_request_bypasses_gzip(self):
        head, _ = self._split(
            self._get("/page.html", accept_encoding="gzip", extra=b"Range: bytes=0-9\r\n")
        )
        self.assertIn(b"206", head.split(b"\r\n", 1)[0])
        self.assertNotIn(b"content-encoding", head)
        self.assertIn(b"accept-ranges: bytes", head)

    def test_small_file_not_compressed(self):
        head, _ = self._split(self._get("/tiny.txt", accept_encoding="gzip"))
        self.assertNotIn(b"content-encoding", head)

    def test_incompressible_type_untouched(self):
        head, _ = self._split(self._get("/photo.jpg", accept_encoding="gzip"))
        self.assertNotIn(b"content-encoding", head)
        self.assertNotIn(b"vary", head)  # not a compressible resource → no Vary

    def test_listing_compressed(self):
        head, body = self._split(self._get("/", accept_encoding="gzip"))
        self.assertIn(b"content-encoding: gzip", head)
        self.assertIn(b"vary: accept-encoding", head)
        self.assertIn(b"page.html", gzip.decompress(body))

    def test_conditional_uses_coding_correct_etag(self):
        head, _ = self._split(self._get("/page.html", accept_encoding="gzip"))
        etag = next(
            line.split(b":", 1)[1].strip().decode()
            for line in head.split(b"\r\n")
            if line.startswith(b"etag:")
        )
        self.assertTrue(etag.endswith('-gz"'))
        head2, body2 = self._split(
            self._get(
                "/page.html", accept_encoding="gzip", extra=f"If-None-Match: {etag}\r\n".encode()
            )
        )
        self.assertIn(b"304", head2.split(b"\r\n", 1)[0])
        self.assertEqual(body2, b"")


@unittest.skipUnless(_compress.HAVE_ZSTD, "zstd needs Python 3.14+ (compression.zstd)")
class ZstdServerTest(_ServerCase):
    def _split(self, resp):
        head, _, body = resp.partition(b"\r\n\r\n")
        return head.lower(), body

    def test_compresses_text_with_zstd_when_accepted(self):
        from compression import zstd  # ty: ignore[unresolved-import]

        head, body = self._split(self._get("/page.html", accept_encoding="zstd"))
        self.assertIn(b"content-encoding: zstd", head)
        self.assertIn(b"vary: accept-encoding", head)
        self.assertNotIn(b"accept-ranges", head)  # a coded body is not byte-rangeable
        self.assertIn(b'-zst"', head)  # distinct ETag for the zstd representation
        self.assertEqual(zstd.decompress(body), b"<h1>hi</h1>\n" + b"x" * 4000)

    def test_zstd_preferred_over_gzip(self):
        head, _ = self._split(self._get("/page.html", accept_encoding="gzip, zstd"))
        self.assertIn(b"content-encoding: zstd", head)

    def test_gzip_still_served_when_only_gzip_accepted(self):
        head, _ = self._split(self._get("/page.html", accept_encoding="gzip"))
        self.assertIn(b"content-encoding: gzip", head)

    def test_listing_uses_zstd(self):
        head, _ = self._split(self._get("/", accept_encoding="zstd"))
        self.assertIn(b"content-encoding: zstd", head)


class NoCompressTest(_ServerCase):
    compress = False

    def test_no_compress_disables_gzip(self):
        head, _ = self._get("/page.html", accept_encoding="gzip").partition(b"\r\n\r\n")[0], None
        self.assertNotIn(b"content-encoding: gzip", head.lower())

    def test_no_compress_disables_zstd(self):
        head, _ = self._get("/page.html", accept_encoding="zstd").partition(b"\r\n\r\n")[0], None
        self.assertNotIn(b"content-encoding:", head.lower())


class WithCharsetTest(unittest.TestCase):
    def test_text_types_get_utf8(self):
        for ctype in ("text/markdown", "text/plain", "text/html", "text/csv", "text/javascript"):
            self.assertEqual(_compress.with_charset(ctype), f"{ctype}; charset=utf-8")

    def test_structured_text_types_get_utf8(self):
        for ctype in (
            "application/json",
            "image/svg+xml",
            "application/xml",
            "application/ld+json",
        ):
            self.assertEqual(_compress.with_charset(ctype), f"{ctype}; charset=utf-8")

    def test_binary_types_unchanged(self):
        for ctype in ("image/png", "application/octet-stream", "font/woff2", "video/mp4"):
            self.assertEqual(_compress.with_charset(ctype), ctype)

    def test_already_parameterized_unchanged(self):
        self.assertEqual(
            _compress.with_charset("text/html; charset=iso-8859-1"),
            "text/html; charset=iso-8859-1",
        )

    def test_empty_unchanged(self):
        self.assertEqual(_compress.with_charset(""), "")


if __name__ == "__main__":
    unittest.main()
