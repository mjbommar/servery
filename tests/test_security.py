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


if __name__ == "__main__":
    unittest.main()
