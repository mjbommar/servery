"""Upload / multipart robustness tests (RFC 7578, RFC 5987/6266) + httpx interop."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from servery import upload
from servery.config import Config
from tests._harness import raw_exchange, serving

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


def _part(name: str, disposition_extra: str, content: bytes) -> bytes:
    return (
        (
            f'--B\r\nContent-Disposition: form-data; name="{name}"{disposition_extra}\r\n\r\n'
        ).encode()
        + content
        + b"\r\n"
    )


class FilenameParsingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _save(self, body: bytes):
        return upload.save(io.BytesIO(body), b"B", str(self.dir))

    def test_rfc5987_filename_star(self):
        body = _part("f", "; filename*=UTF-8''caf%C3%A9.txt", b"DATA") + b"--B--\r\n"
        saved = self._save(body)
        self.assertEqual([s.filename for s in saved], ["café.txt"])
        self.assertEqual((self.dir / "café.txt").read_bytes(), b"DATA")

    def test_filename_star_preferred_over_plain(self):
        body = (
            _part("f", "; filename=\"fallback.txt\"; filename*=UTF-8''real.txt", b"X")
            + b"--B--\r\n"
        )
        saved = self._save(body)
        self.assertEqual([s.filename for s in saved], ["real.txt"])

    def test_bad_filename_star_falls_back_to_plain(self):
        # Unknown charset -> ignore extended, use plain.
        body = _part("f", "; filename=\"ok.txt\"; filename*=BOGUS''x.txt", b"X") + b"--B--\r\n"
        saved = self._save(body)
        self.assertEqual([s.filename for s in saved], ["ok.txt"])

    def test_plain_unicode_filename(self):
        body = _part("f", '; filename="naïve.txt"', b"X") + b"--B--\r\n"
        saved = self._save(body)
        self.assertEqual([s.filename for s in saved], ["naïve.txt"])

    def test_multiple_files(self):
        body = (
            _part("a", '; filename="a.txt"', b"AAA")
            + _part("b", '; filename="b.bin"', b"BBBB")
            + b"--B--\r\n"
        )
        saved = self._save(body)
        self.assertEqual({s.filename for s in saved}, {"a.txt", "b.bin"})
        self.assertEqual((self.dir / "a.txt").read_bytes(), b"AAA")

    def test_boundary_string_inside_content(self):
        # An embedded "--B" not preceded by CRLF must not split the part.
        body = _part("f", '; filename="x.bin"', b"AA--B-not-real-BB") + b"--B--\r\n"
        self._save(body)
        self.assertEqual((self.dir / "x.bin").read_bytes(), b"AA--B-not-real-BB")

    def test_part_with_own_content_type(self):
        body = (
            b'--B\r\nContent-Disposition: form-data; name="f"; filename="x.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\nDATA\r\n--B--\r\n"
        )
        self._save(body)
        self.assertEqual((self.dir / "x.txt").read_bytes(), b"DATA")

    def test_empty_upload_saves_nothing(self):
        self.assertEqual(self._save(b"--B--\r\n"), [])


class BoundaryExtractionTest(unittest.TestCase):
    def test_boundary_with_extra_params(self):
        self.assertEqual(
            upload.extract_boundary("multipart/form-data; charset=utf-8; boundary=XYZ"), b"XYZ"
        )
        self.assertEqual(upload.extract_boundary('multipart/form-data; boundary="a b"'), b"a b")


class UploadServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.cfg = Config.create(self.dir, host="127.0.0.1", port=0, quiet=True, upload=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_chunked_upload_rejected_cleanly(self):
        # We require Content-Length; a chunked body must be refused (411), not hang.
        with serving(self.cfg) as (host, port):
            request = (
                b"POST / HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: multipart/form-data; boundary=B\r\n"
                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                b"5\r\nhello\r\n0\r\n\r\n"
            )
            resp = raw_exchange(host, port, request)
            status = int(resp.split(b"\r\n", 1)[0].split(b" ")[1])
            self.assertEqual(status, 411)

    @unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
    def test_httpx_multipart_upload(self):
        with serving(self.cfg) as (host, port):
            with httpx.Client() as client:
                resp = client.post(
                    f"http://{host}:{port}/", files={"file": ("report.txt", b"payload")}
                )
            self.assertEqual(resp.status_code, 303)
            self.assertEqual((self.dir / "report.txt").read_bytes(), b"payload")

    @unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
    def test_httpx_multiple_files(self):
        with serving(self.cfg) as (host, port):
            with httpx.Client() as client:
                resp = client.post(
                    f"http://{host}:{port}/",
                    files=[
                        ("file", ("one.txt", b"ONE")),
                        ("file", ("two.txt", b"TWO")),
                    ],
                )
            self.assertEqual(resp.status_code, 303)
            self.assertEqual((self.dir / "one.txt").read_bytes(), b"ONE")
            self.assertEqual((self.dir / "two.txt").read_bytes(), b"TWO")


if __name__ == "__main__":
    unittest.main()
