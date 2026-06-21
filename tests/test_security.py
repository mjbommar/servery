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


if __name__ == "__main__":
    unittest.main()
