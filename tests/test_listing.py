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


class HelperTest(unittest.TestCase):
    def test_relative_time_tiers(self):
        now = 1_000_000_000.0
        rt = listing._relative_time
        self.assertEqual(rt(now, now), "just now")
        self.assertEqual(rt(now, now + 5), "just now")  # clamps negative deltas
        self.assertEqual(rt(now - 120, now), "2m ago")
        self.assertEqual(rt(now - 7200, now), "2h ago")
        self.assertEqual(rt(now - 3 * 86400, now), "3d ago")
        self.assertEqual(rt(now - 21 * 86400, now), "3w ago")
        self.assertEqual(rt(now - 120 * 86400, now), "4mo ago")
        self.assertEqual(rt(now - 800 * 86400, now), "2y ago")

    def test_extension(self):
        self.assertEqual(listing._extension("photo.JPG"), "jpg")
        self.assertEqual(listing._extension("archive.tar.gz"), "gz")
        self.assertEqual(listing._extension("Makefile"), "")
        self.assertEqual(listing._extension(".bashrc"), "")

    def test_category(self):
        def info(name, is_dir=False):
            return listing.EntryInfo(name, is_dir, False, 0, 0.0)

        self.assertEqual(listing._category(info("x", is_dir=True)), "dir")
        self.assertEqual(listing._category(info("a.png")), "image")
        self.assertEqual(listing._category(info("a.unknownext")), "binary")

    def test_category_mimetypes_fallback(self):
        # Extensions not in the hand-curated table fall back to mimetypes — still
        # a pure extension lookup, no file content read.
        self.assertEqual(listing._ext_to_category("docx"), "doc")  # curated
        self.assertEqual(listing._ext_to_category("tiff"), "image")  # curated
        # mimetypes-only (absent from _EXT_CATEGORY):
        self.assertEqual(listing._ext_to_category("mpeg"), "video")
        self.assertEqual(listing._ext_to_category("aiff"), "audio")
        self.assertEqual(listing._ext_to_category(""), "binary")
        self.assertNotIn("mpeg", listing._EXT_CATEGORY)  # guard: truly a fallback


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

    @unittest.skipIf(os.name == "nt", "< > & are illegal in Windows filenames")
    def test_html_escaping(self):
        (self.dir / "a&b<c>.txt").write_text("x")
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn("a&amp;b&lt;c&gt;", body)
        self.assertNotIn("a&b<c>.txt", body)

    @unittest.skipIf(os.name == "nt", '" is illegal in Windows filenames')
    def test_href_is_percent_encoded_not_raw(self):
        # The href no longer goes through html.escape; quote() must encode every
        # attribute-breaking / XSS character so a hostile name can't escape.
        (self.dir / 'x" onmouseover=alert(1).txt').write_text("x")
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertNotIn('" onmouseover=', body)  # the raw attribute breakout
        self.assertIn("%22%20onmouseover", body)  # encoded instead

    def test_mtime_format(self):
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertRegex(body, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_sortable_headers_present(self):
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn("C=N", body)
        self.assertIn("C=S", body)
        self.assertIn("C=M", body)
        self.assertIn('type="search"', body)

    def test_sort_by_size_desc(self):
        (self.dir / "big.bin").write_bytes(b"x" * 5000)
        (self.dir / "tiny.bin").write_bytes(b"x")
        body = listing.render(str(self.dir), "/", show_hidden=False, sort="size", order="desc")
        text = body.decode("utf-8")
        self.assertLess(text.index("big.bin"), text.index("tiny.bin"))

    def test_query_filters(self):
        (self.dir / "needle.txt").write_text("x")
        body = listing.render(str(self.dir), "/", show_hidden=False, query="needle").decode("utf-8")
        self.assertIn("needle.txt", body)
        self.assertNotIn("alpha.txt", body)

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

    def test_breadcrumb_links_each_segment(self):
        body = listing.render(str(self.dir), "/foo/bar/", show_hidden=False).decode("utf-8")
        # Intermediate segment is a link; the current dir is plain text.
        self.assertIn('<a href="/foo/">foo</a>', body)
        self.assertIn('<span class="here">bar</span>', body)

    def test_file_type_icon_present(self):
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn('class="icon"', body)

    def test_relative_time_with_exact_title(self):
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        # Visible label is relative; the exact timestamp lives in the title.
        self.assertRegex(body, r'title="\d{4}-\d{2}-\d{2} \d{2}:\d{2}"')
        self.assertRegex(body, r"\d+[a-z]+ ago|just now")

    def test_per_file_download_link(self):
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn("alpha.txt?download=1", body)
        self.assertIn("download ", body)  # the HTML download attribute

    def test_extension_facets_and_filter(self):
        (self.dir / "a.py").write_text("x")
        (self.dir / "b.py").write_text("y")
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn("ext=py", body)  # a facet chip exists
        # Filtering by ext keeps matches + directories, drops non-matches.
        filtered = listing.render(str(self.dir), "/", show_hidden=False, ext="py").decode("utf-8")
        self.assertIn("a.py", filtered)
        self.assertNotIn("alpha.txt", filtered)
        self.assertIn("subdir/", filtered)  # dirs survive a type filter

    def test_metrics_strip_has_totals(self):
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn('class="metrics"', body)
        self.assertIn("file(s)", body)
        self.assertIn("total", body)

    def test_timeline_svg_rendered(self):
        for i in range(5):
            (self.dir / f"f{i}.bin").write_bytes(b"x" * i)
        body = listing.render(str(self.dir), "/", show_hidden=False).decode("utf-8")
        self.assertIn('class="timeline"', body)
        self.assertIn("<svg", body)
        self.assertIn("<rect", body)

    def test_pagination_splits_pages(self):
        for i in range(25):
            (self.dir / f"item{i:02d}.dat").write_text("x")
        page1 = listing.render(str(self.dir), "/", show_hidden=False, per_page=10, page=1).decode(
            "utf-8"
        )
        self.assertIn('class="pager"', page1)
        self.assertIn("page=2", page1)
        page3 = listing.render(str(self.dir), "/", show_hidden=False, per_page=10, page=3).decode(
            "utf-8"
        )
        # Page 3 shows the tail and offers no "next".
        self.assertIn("item24.dat", page3)
        self.assertNotIn("item00.dat", page3)

    def test_theme_attribute_and_links(self):
        body = listing.render(str(self.dir), "/", show_hidden=False, theme="dark").decode("utf-8")
        self.assertIn('data-theme="dark"', body)
        self.assertIn("theme=light", body)  # the toggle offers other themes

    def test_empty_filter_state(self):
        body = listing.render(str(self.dir), "/", show_hidden=False, query="zzz-no-match").decode(
            "utf-8"
        )
        self.assertIn("No items match", body)
        self.assertIn("Clear filters", body)

    def test_no_javascript_in_output(self):
        body = listing.render(str(self.dir), "/", show_hidden=False, upload=True).decode("utf-8")
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("javascript:", body.lower())
        self.assertNotIn("onclick", body.lower())


if __name__ == "__main__":
    unittest.main()
