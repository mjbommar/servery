"""Tests for the shared buffered-response builder (servery._response).

This is the single source of truth for the content-coding decision + policy headers
used by the HTTP/2 and HTTP/3 backends; lock its contract here so the two transports
can't drift.
"""

from __future__ import annotations

import gzip
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from servery import _compress, _response
from servery.config import Config


def _headers_dict(headers):
    return dict(headers)


class ShouldGzipTest(unittest.TestCase):
    """The single gzip decision (shared by every transport)."""

    BIG = _compress.GZIP_MIN + 100

    def test_compressible_in_band_accepted(self):
        self.assertTrue(_compress.should_gzip("text/plain", self.BIG, "gzip", enabled=True))

    def test_client_declines(self):
        self.assertFalse(_compress.should_gzip("text/plain", self.BIG, "", enabled=True))

    def test_incompressible_type(self):
        self.assertFalse(_compress.should_gzip("image/png", self.BIG, "gzip", enabled=True))

    def test_too_small(self):
        self.assertFalse(_compress.should_gzip("text/plain", 10, "gzip", enabled=True))

    def test_disabled(self):
        self.assertFalse(_compress.should_gzip("text/plain", self.BIG, "gzip", enabled=False))


class FinalizeBodyTest(unittest.TestCase):
    def test_gzip_compresses_and_sets_encoding(self):
        body = b"x" * (_compress.GZIP_MIN + 100)
        status, headers, out = _response.finalize_body([], "text/plain", body, coding="gzip")
        h = _headers_dict(headers)
        self.assertEqual(status, 200)
        self.assertEqual(h[b"vary"], b"accept-encoding")  # compressible type
        self.assertEqual(h[b"content-encoding"], b"gzip")
        self.assertEqual(gzip.decompress(out), body)
        self.assertEqual(h[b"content-length"], str(len(out)).encode())

    def test_identity_serves_plain_but_still_varies(self):
        body = b"x" * 100
        _status, headers, out = _response.finalize_body([], "text/plain", body, coding=None)
        h = _headers_dict(headers)
        self.assertEqual(h[b"vary"], b"accept-encoding")  # still advertise negotiation
        self.assertNotIn(b"content-encoding", h)
        self.assertEqual(out, body)

    def test_incompressible_no_vary(self):
        body = b"\x00" * 100
        _status, headers, out = _response.finalize_body([], "image/png", body, coding=None)
        self.assertNotIn(b"vary", _headers_dict(headers))
        self.assertEqual(out, body)


class HeaderAndBuildTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.create(".", quiet=True)

    def test_base_headers_hsts_only_with_tls(self):
        self.assertIn(
            b"strict-transport-security", _headers_dict(_response.base_headers(self.cfg, tls=True))
        )
        self.assertNotIn(
            b"strict-transport-security", _headers_dict(_response.base_headers(self.cfg, tls=False))
        )

    def test_build_static_escaped_path_is_404(self):
        status, _headers, _body = _response.build_static(self.cfg, "", "/x", "", tls=True)
        self.assertEqual(status, 404)

    def test_build_static_file_and_dir(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("hello")
            cfg = Config.create(d, quiet=True)
            status, headers, body = _response.build_static(
                cfg, str(Path(d) / "a.txt"), "/a.txt", "", tls=True
            )
            h = _headers_dict(headers)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"hello")
            self.assertIn(b"etag", h)  # the buffered backends now send validators
            self.assertIn(b"last-modified", h)
            # a directory without a trailing slash redirects
            status_dir, headers_dir, _ = _response.build_static(cfg, d, "/sub", "", tls=True)
            self.assertEqual(status_dir, 301)
            self.assertEqual(_headers_dict(headers_dir)[b"location"], b"/sub/")

    def test_listing_error_and_disabled_page_security_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(_response.listing, "render", side_effect=OSError):
                status, _headers, _body = _response.build_static(
                    self.cfg, directory, "/", "", tls=False
                )
            self.assertEqual(status, 404)

            config = Config.create(directory, quiet=True, security_headers=False)
            status, headers, _body = _response.build_static(config, directory, "/", "", tls=False)
            self.assertEqual(status, 200)
            self.assertNotIn(b"content-security-policy", _headers_dict(headers))

    def test_build_static_conditional_304(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("hello world")
            cfg = Config.create(d, quiet=True)
            path = str(Path(d) / "a.txt")
            _status, headers, _body = _response.build_static(cfg, path, "/a.txt", "", tls=True)
            etag = _headers_dict(headers)[b"etag"].decode()
            # Re-request with the ETag -> 304, no body, validators echoed.
            status2, headers2, body2 = _response.build_static(
                cfg, path, "/a.txt", "", tls=True, if_none_match=etag
            )
            self.assertEqual(status2, 304)
            self.assertEqual(body2, b"")
            self.assertEqual(_headers_dict(headers2)[b"etag"].decode(), etag)
            # A non-matching tag still serves the body.
            status3, _h3, body3 = _response.build_static(
                cfg, path, "/a.txt", "", tls=True, if_none_match='"nope"'
            )
            self.assertEqual(status3, 200)
            self.assertEqual(body3, b"hello world")

    def test_build_static_gzip_etag_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "p.txt").write_text("x" * (_compress.GZIP_MIN + 50))
            cfg = Config.create(d, quiet=True)
            path = str(Path(d) / "p.txt")
            _s1, h_plain, _b1 = _response.build_static(cfg, path, "/p.txt", "", tls=True)
            _s2, h_gz, _b2 = _response.build_static(cfg, path, "/p.txt", "gzip", tls=True)
            # The gzip representation carries a distinct ETag (RFC 9110 §8.8.3.3).
            self.assertNotEqual(_headers_dict(h_plain)[b"etag"], _headers_dict(h_gz)[b"etag"])
            self.assertTrue(_headers_dict(h_gz)[b"etag"].endswith(b'-gz"'))

    def test_large_file_uses_identity_file_body_above_buffer_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "large.txt")
            path.write_bytes(b"x" * 4096)
            cfg = Config.create(
                tmp,
                quiet=True,
                max_buffered_response=1024,
                max_compress_size=10_000,
            )
            status, headers, body = _response.build_static(
                cfg, str(path), "/large.txt", "gzip", tls=True
            )
            self.assertEqual(status, 200)
            self.assertIsInstance(body, _response.FileBody)
            assert isinstance(body, _response.FileBody)
            self.assertEqual((body.path, body.size), (str(path), 4096))
            self.assertFalse(body.handle.closed)
            body.close()
            mapped = _headers_dict(headers)
            self.assertEqual(mapped[b"content-length"], b"4096")
            self.assertNotIn(b"content-encoding", mapped)
            self.assertEqual(mapped[b"vary"], b"accept-encoding")

    def test_zero_threshold_forces_nonempty_file_streaming(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "one.bin")
            path.write_bytes(b"x")
            cfg = Config.create(tmp, quiet=True, max_buffered_response=0)
            _status, _headers, body = _response.build_static(
                cfg, str(path), "/one.bin", "", tls=False
            )
            self.assertIsInstance(body, _response.FileBody)
            assert isinstance(body, _response.FileBody)
            body.close()

    def test_large_binary_stream_has_no_accept_encoding_vary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "large.bin")
            path.write_bytes(b"x" * 10)
            cfg = Config.create(tmp, quiet=True, max_buffered_response=1)
            _status, headers, body = _response.build_static(
                cfg, str(path), "/large.bin", "gzip", tls=False
            )
            self.assertIsInstance(body, _response.FileBody)
            assert isinstance(body, _response.FileBody)
            body.close()
            self.assertNotIn(b"vary", _headers_dict(headers))

    @unittest.skipIf(os.name == "nt", "Windows cannot replace an open file")
    def test_streaming_body_keeps_the_validated_open_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "asset.bin")
            original = b"a" * 4096
            path.write_bytes(original)
            cfg = Config.create(tmp, quiet=True, max_buffered_response=1)

            _status, headers, body = _response.build_static(
                cfg, str(path), "/asset.bin", "", tls=False
            )
            self.assertIsInstance(body, _response.FileBody)
            assert isinstance(body, _response.FileBody)
            replacement = Path(tmp, "replacement.bin")
            replacement.write_bytes(b"replacement")
            replacement.replace(path)
            try:
                self.assertEqual(body.handle.read(), original)
                self.assertEqual(_headers_dict(headers)[b"content-length"], b"4096")
            finally:
                body.close()

    def test_file_open_failure_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp, "small.txt")
            small.write_bytes(b"x")
            cfg = Config.create(tmp, quiet=True)
            with mock.patch("builtins.open", side_effect=OSError):
                status, _headers, _body = _response.build_static(
                    cfg, str(small), "/small.txt", "", tls=False
                )
            self.assertEqual(status, 404)

            encoded = Path(tmp, "encoded.txt")
            encoded.write_bytes(b"x" * (_compress.GZIP_MIN + 100))
            cache = _compress.CompressionCache(1024 * 1024)
            with mock.patch("builtins.open", side_effect=OSError):
                status, _headers, _body = _response.build_static(
                    cfg,
                    str(encoded),
                    "/encoded.txt",
                    "gzip",
                    tls=False,
                    compression_cache=cache,
                )
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
