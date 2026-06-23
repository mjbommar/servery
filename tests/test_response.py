"""Tests for the shared buffered-response builder (servery._response).

This is the single source of truth for the content-coding decision + policy headers
used by the HTTP/2 and HTTP/3 backends; lock its contract here so the two transports
can't drift.
"""

from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from servery import _compress, _response
from servery.config import Config


def _headers_dict(headers):
    return {name: value for name, value in headers}


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
    def test_gzip_true_compresses_and_sets_encoding(self):
        body = b"x" * (_compress.GZIP_MIN + 100)
        status, headers, out = _response.finalize_body([], "text/plain", body, gzip=True)
        h = _headers_dict(headers)
        self.assertEqual(status, 200)
        self.assertEqual(h[b"vary"], b"accept-encoding")  # compressible type
        self.assertEqual(h[b"content-encoding"], b"gzip")
        self.assertEqual(gzip.decompress(out), body)
        self.assertEqual(h[b"content-length"], str(len(out)).encode())

    def test_gzip_false_serves_identity_but_still_varies(self):
        body = b"x" * 100
        _status, headers, out = _response.finalize_body([], "text/plain", body, gzip=False)
        h = _headers_dict(headers)
        self.assertEqual(h[b"vary"], b"accept-encoding")  # still advertise negotiation
        self.assertNotIn(b"content-encoding", h)
        self.assertEqual(out, body)

    def test_incompressible_no_vary(self):
        body = b"\x00" * 100
        _status, headers, out = _response.finalize_body([], "image/png", body, gzip=False)
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


if __name__ == "__main__":
    unittest.main()
