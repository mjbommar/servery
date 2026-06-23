"""Tests for the HTTP/3 request helpers (pure stdlib; no aioquic needed)."""

import os
import tempfile
import unittest
from pathlib import Path

from servery import http3
from servery.config import Config


class Http3HelpersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "hello.txt").write_text("hi there")
        (self.dir / "sub").mkdir()
        self.config = Config.create(self.dir, host="127.0.0.1", port=0, quiet=True)
        self.root_real = os.path.realpath(self.config.directory)

    def tearDown(self):
        self._tmp.cleanup()

    def _build(self, method, path):
        return http3.build_response(self.config, self.root_real, method, path)

    def test_file(self):
        status, _, body = self._build("GET", "/hello.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hi there")

    def test_listing(self):
        status, _, body = self._build("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"hello.txt", body)

    def test_missing_is_404(self):
        status, _, _ = self._build("GET", "/does-not-exist")
        self.assertEqual(status, 404)

    def test_method_not_allowed(self):
        status, _, _ = self._build("POST", "/hello.txt")
        self.assertEqual(status, 405)

    def test_directory_redirect(self):
        status, headers, _ = self._build("GET", "/sub")
        self.assertEqual(status, 301)
        self.assertIn((b"location", b"/sub/"), headers)

    def test_gzip_when_accepted(self):
        import gzip

        (self.dir / "big.txt").write_text("z" * 4000)  # compressible, > 1 KiB
        status, headers, body = http3.build_response(
            self.config, self.root_real, "GET", "/big.txt", "gzip"
        )
        self.assertEqual(status, 200)
        self.assertIn((b"content-encoding", b"gzip"), headers)
        self.assertIn((b"vary", b"accept-encoding"), headers)
        self.assertEqual(gzip.decompress(body), b"z" * 4000)
        # Same file without gzip acceptance: identity, but still Vary-keyed.
        _, headers2, body2 = http3.build_response(
            self.config, self.root_real, "GET", "/big.txt", ""
        )
        self.assertNotIn((b"content-encoding", b"gzip"), headers2)
        self.assertIn((b"vary", b"accept-encoding"), headers2)
        self.assertEqual(body2, b"z" * 4000)

    def test_serve_requires_aioquic(self):
        # aioquic is an optional extra; without it, fail with a clear error.
        with self.assertRaises(http3.Http3UnavailableError):
            http3.serve_http3(self.config)


if __name__ == "__main__":
    unittest.main()
