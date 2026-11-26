"""Path-containment / symlink-escape regression tests."""

import os
import tempfile
import unittest
from pathlib import Path

from servery import security


class ContainmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.root_real = os.path.realpath(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_file_inside_is_contained(self):
        target = self.root / "a.txt"
        target.write_text("x")
        self.assertTrue(security.is_contained(self.root_real, str(target)))

    def test_root_itself_is_contained(self):
        self.assertTrue(security.is_contained(self.root_real, str(self.root)))

    def test_absolute_outside_path_is_rejected(self):
        self.assertFalse(security.is_contained(self.root_real, "/etc/passwd"))

    def test_sibling_prefix_is_rejected(self):
        # /tmp/x/root vs /tmp/x/rootEVIL — commonpath catches what startswith misses.
        evil = self.root_real + "EVIL"
        self.assertFalse(security.is_contained(self.root_real, evil))

    def test_mixed_relative_root_is_rejected(self):
        # commonpath raises ValueError for a relative/absolute mix -> fail closed.
        self.assertFalse(security.is_contained("relative-root", "/etc/passwd"))

    @unittest.skipUnless(os.name == "posix", "POSIX filesystem-root semantics")
    def test_filesystem_root_contains_everything(self):
        # Serving "/" (root_real == os.sep): the separator-strip must still match
        # every absolute descendant (and the root itself).
        self.assertTrue(security.is_contained("/", "/etc/passwd"))
        self.assertTrue(security.is_contained("/", "/"))

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_symlink_escape_is_rejected(self):
        link = self.root / "escape"
        try:
            link.symlink_to("/etc")
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            self.skipTest("symlink creation not permitted")
        self.assertFalse(security.is_contained(self.root_real, str(link / "passwd")))

    def test_contained_path_helper(self):
        target = self.root / "b.txt"
        target.write_text("y")
        self.assertEqual(security.contained_path(self.root, str(target)), str(target))
        self.assertIsNone(security.contained_path(self.root, "/etc/passwd"))

    def test_safe_segments_strips_traversal_query_fragment(self):
        self.assertEqual(security.safe_segments("/a/b/c.txt?x=1#f"), ["a", "b", "c.txt"])
        self.assertEqual(security.safe_segments("/../../etc/passwd"), ["etc", "passwd"])
        self.assertEqual(security.safe_segments("/a/./b/"), ["a", "b"])
        self.assertEqual(security.safe_segments("/a%2Fb"), ["a", "b"])  # %2F decodes then splits

    def test_safe_join_contains_and_rejects(self):
        target = self.root / "sub" / "f.txt"
        target.parent.mkdir()
        target.write_text("z")
        self.assertEqual(security.safe_join(self.root_real, "/sub/f.txt"), str(target))
        # traversal can never escape the root
        self.assertEqual(
            security.safe_join(self.root_real, "/../../../etc/passwd"),
            str(Path(self.root_real, "etc", "passwd")),  # contained, just doesn't exist
        )


class TraversalPolicyTest(unittest.TestCase):
    def test_symlinks_require_opt_in(self):
        from servery.config import Config

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root = str(Path(root).resolve())
            outside = str(Path(outside).resolve())
            target = Path(outside, "file.txt")
            target.write_text("linked content")
            link = Path(root, "link")
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation not permitted")
            path = str(link / "file.txt")
            self.assertFalse(security.is_contained(root, path, Config.create(root)))
            self.assertTrue(
                security.is_contained(root, path, Config.create(root, follow_symlinks=True))
            )
            self.assertFalse(
                security.is_contained(root, str(target), Config.create(root, follow_symlinks=True))
            )

    def test_filesystem_crossing_requires_opt_in(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from servery.config import Config

        with tempfile.TemporaryDirectory() as root:
            root = str(Path(root).resolve())
            child = str(Path(root, "mount", "file"))

            def stat(path, **kwargs):
                return SimpleNamespace(st_dev=1 if str(path) == root else 2, st_mode=0o100644)

            default = Config.create(root)
            enabled = Config.create(root, cross_filesystems=True)
            with patch("servery.security.Path.stat", side_effect=stat, autospec=True):
                self.assertFalse(security.is_contained(root, child, default))
                self.assertTrue(security.is_contained(root, child, enabled))

    def test_http_and_archives_follow_policy(self):
        import io
        import tarfile
        import zipfile

        from servery import archive
        from servery.config import Config
        from tests._harness import get_raw, serving

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root = str(Path(root).resolve())
            outside = str(Path(outside).resolve())
            Path(outside, "file.txt").write_text("linked content")
            try:
                Path(root, "link").symlink_to(outside, target_is_directory=True)
                Path(outside, "cycle").symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation not permitted")
            for enabled in (False, True):
                config = Config.create(root, port=0, quiet=True, follow_symlinks=enabled)
                with serving(config) as (host, port):
                    response = get_raw(host, port, "/link/file.txt")
                    self.assertIn(b"200 OK" if enabled else b"404", response)
                    listing = get_raw(host, port, "/")
                    self.assertEqual(b"link/" in listing, enabled)
                output = io.BytesIO()
                archive.stream_zip(root, "share", output, config)
                with zipfile.ZipFile(output) as zipped:
                    self.assertEqual("share/link/file.txt" in zipped.namelist(), enabled)
                output = io.BytesIO()
                archive.stream_zip_selection(root, ["link"], "share", output, config)
                with zipfile.ZipFile(output) as zipped:
                    self.assertEqual("share/link/file.txt" in zipped.namelist(), enabled)
                output = io.BytesIO()
                archive.stream_targz(root, "share", output, config)
                output.seek(0)
                with tarfile.open(fileobj=output, mode="r:gz") as tar:
                    self.assertEqual("share/link/file.txt" in tar.getnames(), enabled)
                    if enabled:
                        member = tar.getmember("share/link/file.txt")
                        self.assertTrue(member.isfile())
                        extracted = tar.extractfile(member)
                        assert extracted is not None
                        self.assertEqual(extracted.read(), b"linked content")


if __name__ == "__main__":
    unittest.main()
