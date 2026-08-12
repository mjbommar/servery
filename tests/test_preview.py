"""Preview-page and metadata-view tests, unit and end-to-end.

Both features are opt-in, so the gating tests (nothing happens without
``--preview`` / ``--metadata``) matter as much as the rendering ones.
"""

import http.client
import json
import os
import struct
import tempfile
import threading
import unittest
import unittest.mock
import zlib
from pathlib import Path

from servery import _metadata, _preview, cli, listing
from servery.config import Config
from servery.server import make_server

_MARKDOWN = """---
title: The Title
author: Ada Lovelace
tags: [alpha, beta]
---

# Heading

Body with **bold** and `code`.

```python
def f(): return 1
```
"""


def _png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 3, 8, 2, 0, 0, 0))
        + chunk(b"IEND", b"")
    )


class KindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "a.md": "markdown",
            "a.ipynb": "notebook",
            "a.json": "json",
            "a.csv": "table",
            "a.png": "image",
            "a.mp3": "audio",
            "a.mp4": "video",
            "a.pdf": "pdf",
            "a.py": "code",
            "a.txt": "text",
            "README": "text",
            "a.bin": "binary",
            "a.zip": "binary",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_preview.kind_for(name), expected)

    def test_previewable(self):
        self.assertTrue(_preview.previewable("notes.md"))
        self.assertFalse(_preview.previewable("blob.zip"))

    def test_modes(self):
        self.assertEqual(_preview.modes_for("markdown"), ("render", "source"))
        self.assertEqual(_preview.modes_for("code"), ("source",))
        self.assertEqual(_preview.modes_for("image"), ())


class RenderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _metadata.cache_clear()

    def write(self, name: str, content: str | bytes) -> str:
        path = self.dir / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return str(path)

    def render(self, name: str, content: str | bytes, **kwargs) -> str:
        path = self.write(name, content)
        return _preview.render(path, "/" + name, **kwargs).decode("utf-8")


class PreviewRenderTest(RenderTestCase):
    def test_markdown_is_rendered_by_default(self):
        page = self.render("a.md", _MARKDOWN)
        self.assertIn('<h1 id="heading">Heading</h1>', page)
        self.assertIn("<strong>bold</strong>", page)
        self.assertIn('data-lang="Python"', page)
        self.assertNotIn("title: The Title", page)  # front matter is not body text

    def test_markdown_source_mode(self):
        page = self.render("a.md", _MARKDOWN, mode="source")
        self.assertNotIn("<h1 id=", page)
        self.assertIn("Heading", page)
        self.assertIn('class="code ln"', page)  # line numbers

    def test_mode_toggle_links_are_offered(self):
        page = self.render("a.md", _MARKDOWN)
        self.assertIn("?preview=source", page)
        self.assertIn("?preview=render", page)

    def test_code_has_no_toggle_and_is_highlighted(self):
        page = self.render("m.py", "def f():\n    return 1\n")
        self.assertIn('<span class="k">def</span>', page)
        self.assertNotIn("?preview=render", page)
        self.assertIn("Python", page)  # the language badge

    def test_json_is_reindented(self):
        page = self.render("d.json", '{"b":2,"a":[1,2]}')
        self.assertIn('<span class="t">&quot;b&quot;</span>', page.replace('"b"', "&quot;b&quot;"))
        self.assertIn('class="code', page)

    def test_invalid_json_still_renders_as_source(self):
        page = self.render("d.json", "{not json")
        self.assertIn("not json", page)

    def test_csv_becomes_a_table(self):
        page = self.render("d.csv", "name,qty\na,1\nb,2\n")
        self.assertIn('<table class="data">', page)
        self.assertIn("<th>name</th>", page)
        self.assertIn("<td>a</td>", page)

    def test_csv_source_mode_is_not_a_table(self):
        page = self.render("d.csv", "name,qty\na,1\n", mode="source")
        self.assertNotIn('<table class="data">', page)

    def test_tsv_uses_tab_delimiter(self):
        page = self.render("d.tsv", "a\tb\n1\t2\n")
        self.assertIn("<th>a</th>", page)
        self.assertIn("<th>b</th>", page)

    def test_notebook_renders_cells(self):
        notebook = json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["# NB\n"]},
                    {
                        "cell_type": "code",
                        "source": ["print(1)\n"],
                        "outputs": [{"output_type": "stream", "text": ["1\n"]}],
                    },
                ],
                "metadata": {"language_info": {"name": "python"}},
            }
        )
        page = self.render("n.ipynb", notebook)
        self.assertIn(">NB</h1>", page)
        self.assertIn('<span class="b">print</span>', page)
        self.assertIn('<pre class="out">1', page)

    def test_image_is_shown_inline(self):
        page = self.render("i.png", _png_bytes())
        self.assertIn('<img src="i.png"', page)

    def test_audio_and_video_get_controls(self):
        self.assertIn("<audio controls", self.render("a.mp3", b"\x00\x01"))
        self.assertIn("<video controls", self.render("v.mp4", b"\x00\x01"))

    def test_pdf_offers_a_download(self):
        page = self.render("d.pdf", b"%PDF-1.4\n")
        self.assertIn("Download PDF", page)
        self.assertIn("?download=1", page)

    def test_binary_extension_gets_a_card(self):
        self.assertIn("binary file", self.render("blob.zip", b"PK\x03\x04"))

    def test_binary_content_in_a_text_extension_gets_a_card(self):
        self.assertIn("binary data", self.render("weird.txt", b"a\x00b"))

    def test_oversized_file_is_not_read(self):
        page = self.render("big.md", "# T\n" + "x" * 5000, max_bytes=100)
        self.assertIn("preview limit", page)
        self.assertNotIn('<div class="md">', page)  # the body was never rendered

    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            _preview.render(str(self.dir / "nope.md"), "/nope.md")

    def test_metadata_panel_only_when_enabled(self):
        self.assertNotIn("Metadata", self.render("a.md", _MARKDOWN))
        page = self.render("b.md", _MARKDOWN, metadata=True)
        self.assertIn("<summary>Metadata</summary>", page)
        self.assertIn("Ada Lovelace", page)

    def test_breadcrumb_and_theme(self):
        path = self.write("a.md", _MARKDOWN)
        page = _preview.render(path, "/docs/a.md", theme="dark").decode("utf-8")
        self.assertIn('data-theme="dark"', page)
        self.assertIn('<a href="/docs/">docs</a>', page)

    def test_hostile_markdown_cannot_inject_script(self):
        page = self.render(
            "x.md",
            "<script>alert(1)</script>\n\n[c](javascript:alert(1))\n\n<img src=x onerror=y>\n",
        )
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("javascript:", page)
        self.assertNotIn("<img src=x", page)  # inert, escaped text — never a tag
        self.assertIn("&lt;img src=x onerror=y&gt;", page)

    def test_crlf_source_renders_without_stray_carriage_returns(self):
        page = self.render("crlf.py", b"x = 1\r\ny = 2\r\n")
        self.assertNotIn("\r", page)
        self.assertIn('<span class="n">1</span>', page)

    @unittest.skipIf(os.name == "nt", "< and > are illegal in Windows filenames")
    def test_hostile_filename_is_escaped_in_the_page(self):
        page = self.render("a<b>&.md", "# hi\n")
        self.assertNotIn("<b>&", page)
        self.assertIn("a&lt;b&gt;&amp;.md", page)


class PreviewBoundsTest(RenderTestCase):
    def test_table_rows_are_capped(self):
        rows = "a,b\n" + "".join(f"{i},{i}\n" for i in range(_preview._MAX_TABLE_ROWS + 50))
        page = self.render("big.csv", rows)
        self.assertIn("Showing the first", page)
        self.assertEqual(page.count("<tr>"), _preview._MAX_TABLE_ROWS + 1)

    def test_table_columns_are_capped(self):
        wide = ",".join(str(i) for i in range(_preview._MAX_TABLE_COLUMNS + 10))
        page = self.render("wide.csv", wide + "\n")
        self.assertEqual(page.count("<th>"), _preview._MAX_TABLE_COLUMNS)

    def test_empty_table(self):
        self.assertIn("No rows.", self.render("empty.csv", "\n\n"))

    def test_notebook_cells_are_capped(self):
        cells = [
            {"cell_type": "code", "source": ["x\n"]}
            for _ in range(_preview._MAX_NOTEBOOK_CELLS + 5)
        ]
        page = self.render("big.ipynb", json.dumps({"cells": cells}))
        self.assertIn("Showing the first", page)

    def test_notebook_output_variants(self):
        notebook = json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": "x",
                        "outputs": [
                            {"output_type": "execute_result", "data": {"text/plain": ["42"]}},
                            {"output_type": "error", "traceback": ["Traceback", "ZeroDivision"]},
                            {"output_type": "display_data", "data": {"image/png": "ignored"}},
                        ],
                    }
                ]
            }
        )
        page = self.render("n.ipynb", notebook)
        self.assertIn("42", page)
        self.assertIn("ZeroDivision", page)

    def test_notebook_output_budget(self):
        big = "y" * (_preview._MAX_OUTPUT_CHARS * 3)
        notebook = json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": "x",
                        "outputs": [{"text": big}, {"text": big}],
                    }
                ]
            }
        )
        page = self.render("n.ipynb", notebook)
        self.assertLess(page.count("y"), _preview._MAX_OUTPUT_CHARS * 2)

    def test_malformed_notebook_falls_back_to_json_source(self):
        self.assertIn("nope", self.render("n.ipynb", "{nope"))
        self.assertIn("cells", self.render("m.ipynb", '{"cells": "not a list"}'))

    def test_unknown_mode_falls_back_to_the_first_available(self):
        page = self.render("a.md", _MARKDOWN, mode="bogus")
        self.assertIn('<h1 id="heading">', page)


class PreviewServerTestCase(unittest.TestCase):
    """End-to-end over a real socket, with both features enabled."""

    preview = True
    metadata = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "post.md").write_text(_MARKDOWN, encoding="utf-8")
        (self.dir / "other.md").write_text(
            "---\ntitle: Zebra\nauthor: Bob\ntags: [beta]\n---\n\nText.\n", encoding="utf-8"
        )
        (self.dir / "mod.py").write_text('"""Doc title."""\n', encoding="utf-8")
        (self.dir / "blob.zip").write_bytes(b"PK\x03\x04binary")
        (self.dir / "sub").mkdir()
        _metadata.cache_clear()

        config = Config.create(
            self.dir,
            host="127.0.0.1",
            port=0,
            quiet=True,
            preview=self.preview,
            metadata=self.metadata,
        )
        self.httpd = make_server(config)
        self.host = str(self.httpd.server_address[0])
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()

    def get(self, target: str) -> tuple[int, dict[str, str], str]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("GET", target, headers={"Accept-Encoding": "identity"})
            response = conn.getresponse()
            body = response.read().decode("utf-8", "replace")
            return response.status, dict(response.getheaders()), body
        finally:
            conn.close()


class PreviewEndpointTest(PreviewServerTestCase):
    def test_preview_page_is_served(self):
        status, headers, body = self.get("/post.md?preview=1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn('<h1 id="heading">Heading</h1>', body)

    def test_preview_page_widens_the_csp_only_for_media(self):
        _, headers, _ = self.get("/post.md?preview=1")
        policy = headers["Content-Security-Policy"]
        self.assertIn("media-src 'self'", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertNotIn("script-src", policy)

    def test_listing_keeps_the_narrow_csp(self):
        _, headers, _ = self.get("/")
        self.assertNotIn("media-src", headers["Content-Security-Policy"])

    def test_source_mode(self):
        _, _, body = self.get("/post.md?preview=source")
        self.assertNotIn("<h1 id=", body)
        self.assertIn('class="code ln"', body)

    def test_falsey_preview_value_serves_the_file(self):
        _, headers, body = self.get("/post.md?preview=0")
        self.assertNotIn("text/html", headers["Content-Type"])
        self.assertIn("title: The Title", body)

    def test_head_request_sends_no_body(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("HEAD", "/post.md?preview=1")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertGreater(int(response.getheader("Content-Length") or 0), 0)
        finally:
            conn.close()

    def test_preview_of_a_missing_file_is_404(self):
        status, headers, _ = self.get("/nope.md?preview=1")
        self.assertEqual(status, 404)
        # Not claimed by the view: a missing path falls through to normal
        # handling, so --spa (and a plain 404) still behave as they always did.
        self.assertNotIn("media-src", headers["Content-Security-Policy"])

    def test_preview_of_a_directory_still_lists_it(self):
        status, headers, body = self.get("/sub/?preview=1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("Index of /sub/", body)

    def test_error_page_after_a_preview_reverts_the_csp(self):
        self.get("/post.md?preview=1")
        _, headers, _ = self.get("/nope.md")
        self.assertNotIn("media-src", headers["Content-Security-Policy"])

    def test_file_metadata_json(self):
        status, headers, body = self.get("/post.md?metadata=1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        document = json.loads(body)
        self.assertEqual(document["metadata"]["title"], "The Title")
        self.assertEqual(document["metadata"]["author"], "Ada Lovelace")
        self.assertEqual(document["path"], "/post.md")

    def test_directory_metadata_json(self):
        status, headers, body = self.get("/?metadata=1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        document = json.loads(body)
        names = [entry["name"] for entry in document["entries"]]
        self.assertIn("post.md", names)
        self.assertIn("sub", names)
        titles = {
            entry["name"]: entry.get("metadata", {}).get("title") for entry in document["entries"]
        }
        self.assertEqual(titles["post.md"], "The Title")

    def test_metadata_for_a_directory_path_without_slash_redirects(self):
        status, headers, _ = self.get("/sub?metadata=1")
        self.assertEqual(status, 301)
        self.assertIn("/sub/?metadata=1", headers["Location"])

    def test_metadata_on_a_missing_file_is_404(self):
        status, _, _ = self.get("/nope.md?metadata=1")
        self.assertEqual(status, 404)


class ListingIntegrationTest(PreviewServerTestCase):
    def test_listing_shows_the_metadata_column(self):
        _, _, body = self.get("/")
        self.assertIn('<td class="meta">', body)
        self.assertIn("The Title", body)
        self.assertIn("Ada Lovelace", body)
        self.assertIn("C=T", body)  # the title sort header
        self.assertIn("C=A", body)  # the author sort header

    def test_listing_shows_preview_links(self):
        _, _, body = self.get("/")
        self.assertIn("post.md?preview=1", body)
        self.assertNotIn("blob.zip?preview=1", body)  # nothing to preview

    def test_tag_chips_filter(self):
        _, _, body = self.get("/")
        self.assertIn("meta=tag%3Aalpha", body)
        _, _, filtered = self.get("/?meta=tag%3Aalpha")
        self.assertIn("post.md", filtered)
        self.assertNotIn("other.md", filtered)

    def test_meta_field_filter(self):
        _, _, body = self.get("/?meta=author%3Abob")
        self.assertIn("other.md", body)
        self.assertNotIn("post.md", body)

    def test_bare_meta_filter_searches_every_field(self):
        _, _, body = self.get("/?meta=lovelace")
        self.assertIn("post.md", body)
        self.assertNotIn("other.md", body)

    def test_meta_filter_with_no_matches_hides_every_file(self):
        # Directories stay so navigation still works while a filter is on.
        _, _, body = self.get("/?meta=nothingmatches")
        self.assertNotIn("post.md", body)
        self.assertNotIn("other.md", body)
        self.assertIn("sub/", body)

    def test_empty_result_offers_a_reset(self):
        _, _, body = self.get("/sub/?meta=nothingmatches")
        self.assertIn("Clear filters", body)

    def test_sort_by_title(self):
        _, _, body = self.get("/?C=T")
        self.assertLess(body.index("post.md"), body.index("other.md"))  # The < Zebra
        _, _, reversed_body = self.get("/?C=T&O=D")
        self.assertLess(reversed_body.index("other.md"), reversed_body.index("post.md"))

    def test_sort_by_author(self):
        _, _, body = self.get("/?C=A")
        self.assertLess(body.index("post.md"), body.index("other.md"))  # Ada < Bob

    def test_untitled_files_sort_last(self):
        _, _, body = self.get("/?C=T")
        self.assertLess(body.index("post.md"), body.index("blob.zip"))


class OptInTest(unittest.TestCase):
    """With the flags off, the query parameters must do nothing at all."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "post.md").write_text(_MARKDOWN, encoding="utf-8")
        config = Config.create(self.dir, host="127.0.0.1", port=0, quiet=True)
        self.httpd = make_server(config)
        self.host = str(self.httpd.server_address[0])
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()

    def get(self, target: str) -> tuple[int, dict[str, str], str]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("GET", target, headers={"Accept-Encoding": "identity"})
            response = conn.getresponse()
            return (
                response.status,
                dict(response.getheaders()),
                response.read().decode("utf-8", "replace"),
            )
        finally:
            conn.close()

    def test_preview_query_serves_the_raw_file(self):
        status, headers, body = self.get("/post.md?preview=1")
        self.assertEqual(status, 200)
        self.assertNotIn("text/html", headers["Content-Type"])
        self.assertIn("title: The Title", body)

    def test_metadata_query_serves_the_raw_file(self):
        _, headers, body = self.get("/post.md?metadata=1")
        self.assertNotIn("application/json", headers["Content-Type"])
        self.assertIn("# Heading", body)

    def test_directory_metadata_query_serves_the_listing(self):
        _, headers, body = self.get("/?metadata=1")
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertNotIn('<td class="meta">', body)

    def test_listing_has_no_metadata_column_or_preview_links(self):
        _, _, body = self.get("/")
        self.assertNotIn('<td class="meta">', body)
        self.assertNotIn("?preview=1", body)
        self.assertNotIn("C=T", body)


class ListingUnitTest(unittest.TestCase):
    """listing.render's metadata/preview knobs, without a socket."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.dir / "a.md").write_text("---\ntitle: Alpha\n---\n", encoding="utf-8")
        (self.dir / "b.md").write_text("---\ntitle: Beta\n---\n", encoding="utf-8")
        _metadata.cache_clear()

    def render(self, **kwargs) -> str:
        return listing.render(str(self.dir), "/", show_hidden=False, **kwargs).decode("utf-8")

    def test_metadata_off_by_default(self):
        body = self.render()
        self.assertNotIn('<td class="meta">', body)
        self.assertNotIn("C=T", body)

    def test_title_sort_falls_back_to_name_without_metadata(self):
        body = self.render(sort="title", order="desc")
        # No metadata -> the T column does not exist; the sort silently becomes name.
        self.assertNotIn("C=T", body)
        self.assertLess(body.index("b.md"), body.index("a.md"))

    def test_meta_query_is_ignored_without_metadata(self):
        body = self.render(meta_query="title:alpha")
        self.assertIn("b.md", body)

    def test_meta_query_filters_with_metadata(self):
        body = self.render(metadata=True, meta_query="title:alpha")
        self.assertIn("a.md", body)
        self.assertNotIn("b.md", body)

    def test_unknown_meta_field_searches_all_fields(self):
        self.assertEqual(listing.parse_meta_query("author:x"), ("author", "x"))
        self.assertEqual(listing.parse_meta_query("nosuch:x"), ("", "nosuch:x"))
        self.assertEqual(listing.parse_meta_query("plain"), ("", "plain"))

    def test_state_is_preserved_across_sort_links(self):
        body = self.render(metadata=True, meta_query="title:a")
        self.assertIn("meta=title%3Aa", body)

    def test_only_the_visible_page_is_extracted_by_default(self):
        for index in range(6):
            (self.dir / f"f{index}.md").write_text(f"# T{index}\n", encoding="utf-8")
        calls: list[str] = []
        original = _metadata.for_file

        def counting(path, **kwargs):
            calls.append(path)
            return original(path, **kwargs)

        with unittest.mock.patch.object(_metadata, "for_file", counting):
            listing.render(str(self.dir), "/", show_hidden=False, metadata=True, page=1, per_page=2)
        self.assertEqual(len(calls), 2)  # the page, not the directory

    def test_a_metadata_sort_extracts_the_whole_directory(self):
        for index in range(6):
            (self.dir / f"f{index}.md").write_text(f"# T{index}\n", encoding="utf-8")
        calls: list[str] = []
        original = _metadata.for_file

        def counting(path, **kwargs):
            calls.append(path)
            return original(path, **kwargs)

        with unittest.mock.patch.object(_metadata, "for_file", counting):
            listing.render(
                str(self.dir),
                "/",
                show_hidden=False,
                metadata=True,
                sort="title",
                page=1,
                per_page=2,
            )
        self.assertEqual(len(calls), 8)  # a.md, b.md, and the six f*.md

    def test_parent_row_gets_a_blank_metadata_cell(self):
        body = listing.render(str(self.dir), "/sub/", show_hidden=False, metadata=True).decode()
        self.assertIn('<a href="../">../</a></td><td class="meta"></td>', body)


class CliTest(unittest.TestCase):
    def test_flags_default_off(self):
        config = cli.config_from_args(cli.build_parser().parse_args([]))
        self.assertFalse(config.preview)
        self.assertFalse(config.metadata)
        self.assertEqual(config.preview_max_bytes, 2 * 1024 * 1024)
        self.assertEqual(config.metadata_max_bytes, 64 * 1024)

    def test_flags_parse(self):
        args = cli.build_parser().parse_args(
            ["--preview", "--metadata", "--preview-max-bytes", "500", "--metadata-max-bytes", "10"]
        )
        config = cli.config_from_args(args)
        self.assertTrue(config.preview)
        self.assertTrue(config.metadata)
        self.assertEqual(config.preview_max_bytes, 500)
        self.assertEqual(config.metadata_max_bytes, 10)

    def test_non_positive_budgets_are_rejected(self):
        with self.assertRaises(ValueError):
            Config.create(".", preview_max_bytes=0)
        with self.assertRaises(ValueError):
            Config.create(".", metadata_max_bytes=-1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
