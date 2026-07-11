"""Unit tests for on-the-fly directory archives."""

import http.client
import io
import os
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

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


class ArchiveAdmissionTest(unittest.TestCase):
    def test_saturation_rejects_before_headers_preserves_cheap_progress_and_recovers(self):
        from servery.config import Config
        from tests._harness import serving

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "small.txt").write_text("cheap")
            (root / "archive.txt").write_text("archive")
            cfg = Config.create(
                tmp,
                host="127.0.0.1",
                port=0,
                quiet=True,
                max_workers=2,
                max_archive_streams=1,
            )
            entered = threading.Event()
            release = threading.Event()
            original = archive.stream_zip
            calls = 0
            calls_lock = threading.Lock()

            def controlled(*args, **kwargs):
                nonlocal calls
                with calls_lock:
                    calls += 1
                    first = calls == 1
                if first:
                    entered.set()
                    release.wait(2)
                return original(*args, **kwargs)

            first_result: list[int] = []
            with (
                mock.patch.object(archive, "stream_zip", side_effect=controlled),
                serving(cfg) as (
                    host,
                    port,
                ),
            ):

                def first_archive() -> None:
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    try:
                        conn.request("GET", "/?archive=zip")
                        response = conn.getresponse()
                        response.read()
                        first_result.append(response.status)
                    finally:
                        conn.close()

                thread = threading.Thread(target=first_archive)
                thread.start()
                self.assertTrue(entered.wait(1))

                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request("GET", "/?archive=zip")
                saturated = conn.getresponse()
                saturated.read()
                self.assertEqual(saturated.status, 503)
                self.assertEqual(saturated.getheader("Retry-After"), "1")

                conn.request("GET", "/small.txt")
                cheap = conn.getresponse()
                self.assertEqual((cheap.status, cheap.read()), (200, b"cheap"))

                conn.request("HEAD", "/?archive=zip")
                head = conn.getresponse()
                head.read()
                self.assertEqual(head.status, 200)
                conn.close()

                release.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(first_result, [200])

                recovery = http.client.HTTPConnection(host, port, timeout=5)
                recovery.request("GET", "/?archive=zip")
                response = recovery.getresponse()
                response.read()
                recovery.close()
                self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
