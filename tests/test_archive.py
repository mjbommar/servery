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

    def test_zip_selection_includes_chosen_entries(self):
        (self.dir / "c.txt").write_text("C")
        buf = io.BytesIO()
        archive.stream_zip_selection(str(self.dir), ["a.txt", "sub"], "root", buf)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            names = set(zf.namelist())
        self.assertEqual(names, {"root/a.txt", "root/sub/b.txt"})  # c.txt NOT selected

    def test_zip_selection_rejects_escaping_names(self):
        outside = self.dir.parent / "secret.txt"
        outside.write_text("LEAK")
        self.addCleanup(outside.unlink)
        buf = io.BytesIO()
        # Names with separators / ".." are skipped — a crafted selection can't escape.
        archive.stream_zip_selection(str(self.dir), ["../secret.txt", "sub/b.txt", ".."], "r", buf)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            self.assertEqual(zf.namelist(), [])  # nothing escaped, nothing matched

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


class SelectionDownloadTest(unittest.TestCase):
    def test_listing_offers_select_and_zip(self):
        import http.client

        from servery.config import Config
        from tests._harness import serving

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in ("one.txt", "two.txt", "three.txt"):
            (root / name).write_text(name)
        cfg = Config.create(str(root), host="127.0.0.1", port=0, quiet=True)
        with serving(cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/")
            html = conn.getresponse().read().decode()
            self.assertIn('name="sel"', html)  # per-entry checkbox
            self.assertIn('id="zipform"', html)  # the JS-free zip form
            conn.request("GET", "/?sel=one.txt&sel=three.txt")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            data = resp.read()
            conn.close()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertEqual({n.split("/")[-1] for n in zf.namelist()}, {"one.txt", "three.txt"})


if __name__ == "__main__":
    unittest.main()
