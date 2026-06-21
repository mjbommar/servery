"""Directory-listing rendering tests."""

import os
import tempfile
import unittest
from pathlib import Path

from servery import listing


class HumanSizeTest(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(listing._human_size(0), "0 B")
        self.assertEqual(listing._human_size(512), "512 B")

    def test_scaled(self):
        self.assertEqual(listing._human_size(1024), "1.0 KiB")
        self.assertEqual(listing._human_size(1536), "1.5 KiB")
        self.assertEqual(listing._human_size(1048576), "1.0 MiB")


class RenderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "alpha.txt").write_text("hello")
        (self.dir / "subdir").mkdir()
        (self.dir / ".hidden").write_text("secret")

    def tearDown(self):
        self._tmp.cleanup()

    def test_basic_contents(self):
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn("Index of /", body)
        self.assertIn("alpha.txt", body)
        self.assertIn("subdir/", body)
        self.assertNotIn(".hidden", body)

    def test_directories_listed_before_files(self):
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertLess(body.index("subdir/"), body.index("alpha.txt"))

    def test_show_hidden(self):
        body = listing.render(str(self.dir), "/", show_hidden=True).decode("utf-8")
        self.assertIn(".hidden", body)

    def test_parent_link_only_below_root(self):
        root_body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertNotIn('href="../"', root_body)
        sub_body = listing.render(str(self.dir / "subdir"), "/subdir/", show_hidden=False).decode(
            "utf-8"
        )
        self.assertIn('href="../"', sub_body)

    def test_html_escaping(self):
        (self.dir / "a&b<c>.txt").write_text("x")
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn("a&amp;b&lt;c&gt;", body)
        self.assertNotIn("a&b<c>.txt", body)

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_symlink_entry_marked(self):
        link = self.dir / "shortcut"
        try:
            link.symlink_to(self.dir / "alpha.txt")
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            self.skipTest("symlink creation not permitted")
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn("→", body)

    def test_missing_directory_raises(self):
        with self.assertRaises(OSError):
            listing.render(str(self.dir / "nope"), "/nope/", show_hidden=False)


if __name__ == "__main__":
    unittest.main()
