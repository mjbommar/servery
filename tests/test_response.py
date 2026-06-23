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


class FinalizeBodyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.create(".", quiet=True)  # compress=True by default

    def test_compressible_gets_vary_and_gzip(self):
        body = b"x" * (_compress.GZIP_MIN + 100)  # in the gzip size band
        status, headers, out = _response.finalize_body(self.cfg, [], "text/plain", body, "gzip")
        h = _headers_dict(headers)
        self.assertEqual(status, 200)
        self.assertEqual(h[b"vary"], b"accept-encoding")
        self.assertEqual(h[b"content-encoding"], b"gzip")
        self.assertEqual(gzip.decompress(out), body)
        self.assertEqual(h[b"content-length"], str(len(out)).encode())

    def test_compressible_but_client_declines(self):
        body = b"x" * (_compress.GZIP_MIN + 100)
        _status, headers, out = _response.finalize_body(self.cfg, [], "text/plain", body, "")
        h = _headers_dict(headers)
        self.assertEqual(h[b"vary"], b"accept-encoding")  # still advertise negotiation
        self.assertNotIn(b"content-encoding", h)  # but no gzip
        self.assertEqual(out, body)

    def test_incompressible_type_never_gzipped(self):
        body = b"\x00" * (_compress.GZIP_MIN + 100)
        _status, headers, out = _response.finalize_body(self.cfg, [], "image/png", body, "gzip")
        h = _headers_dict(headers)
        self.assertNotIn(b"vary", h)
        self.assertNotIn(b"content-encoding", h)
        self.assertEqual(out, body)

    def test_too_small_to_gzip(self):
        body = b"tiny"  # below GZIP_MIN
        _status, headers, out = _response.finalize_body(self.cfg, [], "text/plain", body, "gzip")
        self.assertNotIn(b"content-encoding", _headers_dict(headers))
        self.assertEqual(out, body)

    def test_compress_disabled(self):
        cfg = Config.create(".", quiet=True, compress=False)
        body = b"x" * (_compress.GZIP_MIN + 100)
        _status, headers, out = _response.finalize_body(cfg, [], "text/plain", body, "gzip")
        self.assertNotIn(b"content-encoding", _headers_dict(headers))
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
            self.assertEqual(status, 200)
            self.assertEqual(body, b"hello")
            # a directory without a trailing slash redirects
            status_dir, headers_dir, _ = _response.build_static(cfg, d, "/sub", "", tls=True)
            self.assertEqual(status_dir, 301)
            self.assertEqual(_headers_dict(headers_dir)[b"location"], b"/sub/")


if __name__ == "__main__":
    unittest.main()
