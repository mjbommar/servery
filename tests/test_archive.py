"""Unit tests for on-the-fly directory archives."""

import io
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from servery import archive


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "a.txt").write_text("AAA")
        sub = self.dir / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("BBBB")

    def tearDown(self):
        self._tmp.cleanup()

    def test_targz_contents(self):
        buf = io.BytesIO()
        archive.stream_targz(str(self.dir), "root", buf)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = set(tar.getnames())
        self.assertIn("root/a.txt", names)
        self.assertIn("root/sub/b.txt", names)

    def test_zip_contents(self):
        buf = io.BytesIO()
        archive.stream_zip(str(self.dir), "root", buf)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            self.assertEqual(zf.read("root/sub/b.txt"), b"BBBB")
            self.assertIn("root/a.txt", zf.namelist())

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_symlinks_are_skipped(self):
        outside = Path(self._tmp.name).parent / "servery_archive_outside.txt"
        outside.write_text("LEAK")
        link = self.dir / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            self.skipTest("symlink creation not permitted")
        try:
            buf = io.BytesIO()
            archive.stream_targz(str(self.dir), "root", buf)
            buf.seek(0)
            with tarfile.open(fileobj=buf, mode="r:gz") as tar:
                names = tar.getnames()
            self.assertNotIn("root/link.txt", names)
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
