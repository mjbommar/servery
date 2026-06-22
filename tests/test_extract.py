"""Secure archive extraction (--upload-extract) tests, incl. the attack vectors."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from servery import _extract
from servery.config import Config
from tests._harness import serving

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class ExtractZipTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write_archive(self, data: bytes) -> str:
        path = Path(self.dest, "a.zip")
        path.write_bytes(data)
        return str(path)

    def test_extracts_nested_files(self):
        archive = self._write_archive(_zip_bytes({"a.txt": b"A", "sub/b.txt": b"B"}))
        names = _extract.extract(archive, self.dest)
        self.assertEqual(set(names), {"a.txt", "sub/b.txt"})
        self.assertEqual(Path(self.dest, "a.txt").read_bytes(), b"A")
        self.assertEqual(Path(self.dest, "sub", "b.txt").read_bytes(), b"B")

    def test_zip_slip_is_rejected(self):
        archive = self._write_archive(_zip_bytes({"../escape.txt": b"pwned"}))
        with self.assertRaises(_extract.ExtractError):
            _extract.extract(archive, self.dest)
        self.assertFalse(Path(self.dest).parent.joinpath("escape.txt").exists())

    def test_absolute_path_is_rejected(self):
        archive = self._write_archive(_zip_bytes({"/tmp/abs_escape.txt": b"x"}))
        with self.assertRaises(_extract.ExtractError):
            _extract.extract(archive, self.dest)

    def test_zip_bomb_size_cap(self):
        # The expanded-size cap is a parameter (no global monkeypatching).
        archive = self._write_archive(_zip_bytes({"big.txt": b"x" * 5000}))
        with self.assertRaises(_extract.ExtractError):
            _extract.extract(archive, self.dest, max_total=1000)
        # And succeeds under a generous cap (overwriting the partial from above).
        self.assertEqual(
            _extract.extract(archive, self.dest, max_total=10_000, allow_overwrite=True),
            ["big.txt"],
        )


class ExtractTarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_files_and_skips_symlinks(self):
        path = Path(self.dest, "a.tar")
        with tarfile.open(path, "w") as tf:
            data = b"hello"
            info = tarfile.TarInfo("real.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("link")  # malicious symlink to an absolute path
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tf.addfile(link)
        names = _extract.extract(str(path), self.dest)
        self.assertEqual(names, ["real.txt"])  # symlink skipped
        self.assertEqual(Path(self.dest, "real.txt").read_bytes(), b"hello")
        self.assertFalse(Path(self.dest, "link").exists())

    def test_tar_slip_is_rejected(self):
        path = Path(self.dest, "evil.tar")
        with tarfile.open(path, "w") as tf:
            data = b"x"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        with self.assertRaises(_extract.ExtractError):
            _extract.extract(str(path), self.dest)


class ConfigTest(unittest.TestCase):
    def test_extract_requires_upload(self):
        with self.assertRaises(ValueError):
            Config.create(".", upload_extract=True)  # no --upload
        Config.create(".", upload=True, upload_extract=True)  # ok


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class UploadExtractEndToEndTest(unittest.TestCase):
    def test_uploaded_zip_is_expanded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config.create(
            tmp.name, host="127.0.0.1", port=0, quiet=True, upload=True, upload_extract=True
        )
        archive = _zip_bytes({"docs/readme.txt": b"hi there"})
        with serving(cfg) as (host, port):
            with httpx.Client() as client:
                resp = client.post(
                    f"http://{host}:{port}/",
                    files={"f": ("bundle.zip", archive, "application/zip")},
                )
            # servery does post/redirect/get after an upload.
            self.assertIn(resp.status_code, (200, 201, 303), resp.text)
            # the archive is gone; its contents are extracted in place
            got = httpx.get(f"http://{host}:{port}/docs/readme.txt")
        self.assertEqual(got.text, "hi there")
        self.assertFalse(Path(tmp.name, "bundle.zip").exists())


if __name__ == "__main__":
    unittest.main()
