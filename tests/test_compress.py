"""On-the-fly gzip content-coding (RFC 9110 §8.4.1.3 / §12.5.3) tests."""

from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from servery import _compress
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
