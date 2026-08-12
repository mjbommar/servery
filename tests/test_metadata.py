"""Metadata extraction tests: per-format extractors, bounds, and the JSON views."""

import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from servery import _metadata


def _png(width: int, height: int, *, text: bytes = b"") -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    body = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    )
    if text:
        body += chunk(b"tEXt", text)
    return body + chunk(b"IEND", b"")


def _id3(frames: list[tuple[bytes, str]]) -> bytes:
    payload = b""
    for name, value in frames:
        data = b"\x03" + value.encode("utf-8")
        size = bytes(((len(data) >> shift) & 0x7F) for shift in (21, 14, 7, 0))
        payload += name + size + b"\x00\x00" + data
    total = bytes(((len(payload) >> shift) & 0x7F) for shift in (21, 14, 7, 0))
    return b"ID3\x04\x00\x00" + total + payload


class ExtractorTestCase(unittest.TestCase):
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

    def meta(self, name: str, content: str | bytes) -> _metadata.Meta:
        return _metadata.extract(self.write(name, content))


class MarkdownTest(ExtractorTestCase):
    def test_yaml_front_matter(self):
        meta = self.meta(
            "post.md",
            "---\ntitle: Hello\nauthor: Ada\ntags: [a, b]\ndate: 2026-01-02\n---\n\nBody text.\n",
        )
        self.assertEqual(meta.kind, "markdown")
        self.assertEqual(meta.title, "Hello")
        self.assertEqual(meta.author, "Ada")
        self.assertEqual(meta.tags, ("a", "b"))
        self.assertEqual(meta.date, "2026-01-02")
        self.assertEqual(meta.description, "Body text.")

    def test_toml_front_matter(self):
        meta = self.meta("post.md", '+++\ntitle = "T"\nauthors = ["X", "Y"]\n+++\n\nBody\n')
        self.assertEqual(meta.title, "T")
        self.assertEqual(meta.author, "X, Y")

    def test_yaml_block_list_and_comments(self):
        meta = self.meta("p.md", "---\ntitle: T  # trailing\nkeywords:\n  - one\n  - two\n---\n")
        self.assertEqual(meta.title, "T")
        self.assertEqual(meta.tags, ("one", "two"))

    def test_heading_fallback(self):
        meta = self.meta("p.md", "# From Heading\n\nAnd a paragraph.\n")
        self.assertEqual(meta.title, "From Heading")
        self.assertEqual(meta.description, "And a paragraph.")

    def test_setext_heading_fallback(self):
        self.assertEqual(self.meta("p.md", "Setext\n======\n").title, "Setext")

    def test_description_skips_badges_and_markup(self):
        meta = self.meta("p.md", "# T\n\nSome **bold** and a [link](https://x.example/).\n")
        self.assertEqual(meta.description, "Some bold and a link.")

    def test_counts_reported(self):
        meta = self.meta("p.md", "# T\n\nword word\n")
        self.assertEqual(dict(meta.extra)["words"], "4")


class SourceAndDataTest(ExtractorTestCase):
    def test_python_docstring_and_dunders(self):
        meta = self.meta(
            "m.py",
            '"""Summary.\n\nDetail.\n"""\n__author__ = "Grace"\n__version__ = "2.1"\n'
            "class A: pass\ndef f(): pass\n",
        )
        self.assertEqual(meta.title, "Summary.")
        self.assertEqual(meta.author, "Grace")
        self.assertEqual(meta.version, "2.1")
        self.assertEqual(meta.description, "Detail.")
        self.assertEqual(dict(meta.extra)["classes"], "1")
        self.assertEqual(dict(meta.extra)["functions"], "1")

    def test_python_with_a_syntax_error_still_returns_counts(self):
        meta = self.meta("bad.py", "def (((\n")
        self.assertEqual(meta.kind, "python")
        self.assertIn("lines", dict(meta.extra))

    def test_latex(self):
        meta = self.meta(
            "p.tex",
            "% \\title{Commented}\n\\documentclass{article}\n"
            "\\title{Real {Nested} Title}\n\\author{Knuth and Lamport}\n",
        )
        self.assertEqual(meta.title, "Real Nested Title")
        self.assertEqual(meta.author, "Knuth, Lamport")
        self.assertEqual(dict(meta.extra)["documentclass"], "article")

    def test_html(self):
        meta = self.meta(
            "p.html",
            '<html lang="fr"><head><title>Titre</title>'
            '<meta name="author" content="Bob">'
            '<meta name="description" content="Une page."></head></html>',
        )
        self.assertEqual(meta.title, "Titre")
        self.assertEqual(meta.author, "Bob")
        self.assertEqual(meta.lang, "fr")

    def test_json_package_style(self):
        meta = self.meta(
            "package.json",
            json.dumps(
                {
                    "name": "thing",
                    "version": "1.2.3",
                    "description": "A thing",
                    "keywords": ["a"],
                    "author": {"name": "Zed"},
                }
            ),
        )
        self.assertEqual(meta.title, "thing")
        self.assertEqual(meta.author, "Zed")
        self.assertEqual(meta.version, "1.2.3")

    def test_toml_pep621(self):
        meta = self.meta(
            "pyproject.toml",
            '[project]\nname = "servery"\nversion = "9.9"\n'
            'description = "d"\nauthors = [{name = "Michael"}]\n',
        )
        self.assertEqual(meta.title, "servery")
        self.assertEqual(meta.author, "Michael")
        self.assertEqual(meta.version, "9.9")

    def test_notebook(self):
        meta = self.meta(
            "nb.ipynb",
            json.dumps(
                {
                    "cells": [{"cell_type": "markdown", "source": ["# NB Title\n"]}],
                    "metadata": {
                        "kernelspec": {"display_name": "Python 3"},
                        "language_info": {"name": "python"},
                    },
                }
            ),
        )
        self.assertEqual(meta.title, "NB Title")
        self.assertEqual(dict(meta.extra)["kernel"], "Python 3")

    def test_csv_header_becomes_tags(self):
        meta = self.meta("d.csv", "name,qty\na,1\nb,2\n")
        self.assertEqual(meta.tags, ("name", "qty"))
        self.assertEqual(dict(meta.extra), {"columns": "2", "rows": "2"})

    def test_rst_title_and_docinfo(self):
        meta = self.meta("d.rst", "Title Here\n==========\n\n:Author: RST Person\n\nbody\n")
        self.assertEqual(meta.title, "Title Here")
        self.assertEqual(meta.author, "RST Person")

    def test_plain_text_first_line_is_the_title(self):
        self.assertEqual(self.meta("n.txt", "Meeting notes\n\nstuff\n").title, "Meeting notes")

    def test_log_files_get_no_title(self):
        self.assertEqual(self.meta("a.log", "2026-01-01 boot\n").title, "")

    def test_unmapped_source_gets_counts_only(self):
        meta = self.meta("a.go", "package main\n")
        self.assertEqual(meta.kind, "source")
        self.assertEqual(meta.title, "")

    def test_yaml_document(self):
        meta = self.meta("c.yaml", "name: thing\nversion: 2\ndescription: d\n")
        self.assertEqual(meta.title, "thing")
        self.assertEqual(meta.version, "2")

    def test_json_array_reports_item_count(self):
        self.assertEqual(dict(self.meta("a.json", "[1, 2, 3]").extra), {"items": "3"})

    def test_toml_cargo_style_package_table(self):
        meta = self.meta("Cargo.toml", '[package]\nname = "crate"\nversion = "0.1"\n')
        self.assertEqual(meta.title, "crate")

    def test_csv_with_inconsistent_quoting_does_not_raise(self):
        self.meta("weird.csv", 'a,"b\nunclosed\n')

    def test_rst_overline_title(self):
        meta = self.meta("o.rst", "======\nOver\n======\n\nbody\n")
        self.assertEqual(meta.title, "Over")


class BinaryFormatTest(ExtractorTestCase):
    def test_png_dimensions_and_text_chunk(self):
        meta = self.meta("i.png", _png(7, 11, text=b"Title\x00Pic"))
        self.assertEqual(dict(meta.extra), {"width": "7", "height": "11"})
        self.assertEqual(meta.title, "Pic")

    def test_gif_dimensions(self):
        meta = self.meta("i.gif", b"GIF89a" + struct.pack("<HH", 5, 9) + b"\x00" * 16)
        self.assertEqual(dict(meta.extra)["width"], "5")

    def test_bmp_dimensions(self):
        header = b"BM" + b"\x00" * 16 + struct.pack("<ii", 3, -4)
        self.assertEqual(dict(self.meta("i.bmp", header).extra), {"width": "3", "height": "4"})

    def test_jpeg_dimensions(self):
        body = b"\xff\xd8" + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 20, 30) + b"\x00" * 12
        self.assertEqual(dict(self.meta("i.jpg", body).extra), {"width": "30", "height": "20"})

    def test_webp_vp8x_dimensions(self):
        body = (
            b"RIFF"
            + b"\x00" * 4
            + b"WEBP"
            + b"VP8X"
            + b"\x00" * 8
            + (11).to_bytes(3, "little")
            + (21).to_bytes(3, "little")
        )
        self.assertEqual(dict(self.meta("i.webp", body).extra), {"width": "12", "height": "22"})

    def test_svg_viewbox_and_title(self):
        meta = self.meta("i.svg", '<svg viewBox="0 0 24 48"><title>Icon</title></svg>')
        self.assertEqual(meta.title, "Icon")
        self.assertEqual(dict(meta.extra), {"width": "24", "height": "48"})

    def test_id3v2_tags(self):
        body = _id3([(b"TIT2", "Song"), (b"TPE1", "Band"), (b"TALB", "Record")])
        meta = self.meta("a.mp3", body + b"\xff\xfb" + b"\x00" * 32)
        self.assertEqual(meta.title, "Song")
        self.assertEqual(meta.author, "Band")
        self.assertEqual(dict(meta.extra)["album"], "Record")

    def test_pdf_info_dictionary(self):
        body = b"%PDF-1.7\n" + b"x" * 40 + b"/Title (Report) /Author (Ann)\ntrailer\n%%EOF\n"
        meta = self.meta("d.pdf", body)
        self.assertEqual(meta.title, "Report")
        self.assertEqual(meta.author, "Ann")
        self.assertEqual(dict(meta.extra)["pdf_version"], "1.7")

    def test_pdf_utf16_hex_string(self):
        raw = "Ünicode".encode("utf-16-be").hex()
        body = ("%PDF-1.4\n/Title <feff" + raw + ">\n").encode("ascii")
        self.assertEqual(self.meta("d.pdf", body).title, "Ünicode")

    def test_truncated_image_returns_empty(self):
        self.assertFalse(self.meta("i.png", b"\x89PNG\r\n\x1a\n"))

    def test_webp_lossy_dimensions(self):
        body = (
            b"RIFF"
            + b"\x00" * 4
            + b"WEBP"
            + b"VP8 "
            + b"\x00" * 8
            + b"\x9d\x01\x2a"
            + struct.pack("<HH", 40, 50)
            + b"\x00" * 8
        )
        self.assertEqual(dict(self.meta("i.webp", body).extra), {"width": "40", "height": "50"})

    def test_webp_lossless_dimensions(self):
        bits = (7 - 1) | ((9 - 1) << 14)
        body = (
            b"RIFF"
            + b"\x00" * 4
            + b"WEBP"
            + b"VP8L"
            + b"\x00" * 4
            + b"\x2f"
            + struct.pack("<I", bits)
        )
        self.assertEqual(dict(self.meta("i.webp", body).extra), {"width": "7", "height": "9"})

    def test_webp_unknown_chunk(self):
        self.assertFalse(
            self.meta("i.webp", b"RIFF" + b"\x00" * 4 + b"WEBP" + b"XXXX" + b"\x00" * 20)
        )

    def test_png_itxt_chunk(self):
        meta = self.meta("i.png", _png(1, 1, text=b"Author\x00Ada"))
        self.assertEqual(meta.author, "Ada")

    def test_id3_v23_frame_sizes(self):
        payload = b"TIT2" + struct.pack(">I", 5) + b"\x00\x00" + b"\x03" + b"Song"
        total = bytes(((len(payload) >> shift) & 0x7F) for shift in (21, 14, 7, 0))
        body = b"ID3\x03\x00\x00" + total + payload
        self.assertEqual(self.meta("a.mp3", body).title, "Song")

    def test_id3_without_known_frames_is_empty(self):
        payload = b"TXXX" + bytes([0, 0, 0, 2]) + b"\x00\x00" + b"\x03x"
        total = bytes(((len(payload) >> shift) & 0x7F) for shift in (21, 14, 7, 0))
        self.assertFalse(self.meta("a.mp3", b"ID3\x04\x00\x00" + total + payload))

    def test_pdf_escapes_in_literal_strings(self):
        body = rb"%PDF-1.5" + b"\n/Title (A \\(B\\) C\\nD)\n"
        self.assertEqual(self.meta("d.pdf", body).title, "A (B) C D")

    def test_svg_width_height_attributes(self):
        meta = self.meta("i.svg", '<svg width="10px" height="20px"></svg>')
        self.assertEqual(dict(meta.extra), {"width": "10px", "height": "20px"})

    def test_non_svg_content_in_an_svg_file(self):
        self.assertFalse(self.meta("i.svg", "not markup at all"))


class RobustnessTest(ExtractorTestCase):
    def test_unknown_extension_is_empty(self):
        self.assertFalse(self.meta("x.bin", b"\x00\x01"))
        self.assertIsNone(_metadata.extractor_for("x.bin"))

    def test_missing_file_is_empty(self):
        self.assertFalse(_metadata.extract(str(self.dir / "nope.md")))
        self.assertFalse(_metadata.for_file(str(self.dir / "nope.md")))

    def test_malformed_inputs_never_raise(self):
        for name, content in (
            ("a.json", "{not json"),
            ("a.toml", "= = ="),
            ("a.ipynb", "[]"),
            ("a.yaml", ": : :"),
            ("a.html", "<html><title>unclosed"),
            ("a.md", "---\nbroken: [\n---\n"),
            ("a.pdf", b"\x00\x01\x02"),
            ("a.mp3", b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f"),
        ):
            with self.subTest(name=name):
                self.meta(name, content)  # must not raise

    def test_read_budget_is_respected(self):
        path = self.write("big.md", "# Title\n\n" + ("x " * 100_000))
        meta = _metadata.extract(path, max_bytes=64)
        self.assertEqual(meta.title, "Title")
        self.assertEqual(meta.extra, ())  # counts suppressed on a partial read

    def test_long_values_are_truncated(self):
        meta = self.meta("p.md", "---\ntitle: " + "z" * 500 + "\n---\n")
        self.assertLessEqual(len(meta.title), 200)

    def test_cache_is_keyed_on_mtime_and_size(self):
        path = self.write("c.md", "# One\n")
        self.assertEqual(_metadata.for_file(path).title, "One")
        Path(path).write_text("# Two and longer\n")
        self.assertEqual(_metadata.for_file(path).title, "Two and longer")


class MetaRecordTest(unittest.TestCase):
    def test_field_lookup_and_haystack(self):
        meta = _metadata.Meta(title="T", author="A", tags=("x", "y"))
        self.assertEqual(meta.field("title"), "T")
        self.assertEqual(meta.field("tag"), "x, y")
        self.assertEqual(meta.field("nothing"), "")
        self.assertIn("x y", meta.haystack())

    def test_bool_and_dict(self):
        self.assertFalse(_metadata.EMPTY)
        self.assertEqual(_metadata.EMPTY.to_dict(), {})
        meta = _metadata.Meta(kind="k", title="T", tags=("a",), extra=(("w", "1"),))
        self.assertTrue(meta)
        self.assertEqual(
            meta.to_dict(), {"kind": "k", "title": "T", "tags": ["a"], "extra": {"w": "1"}}
        )


class JsonViewTest(ExtractorTestCase):
    def test_describe_a_file(self):
        path = self.write("a.md", "# T\n")
        document = _metadata.describe(path, "/a.md")
        self.assertEqual(document["name"], "a.md")
        self.assertEqual(document["type"], "file")
        self.assertEqual(document["metadata"]["title"], "T")
        self.assertIn("content_type", document)
        self.assertRegex(str(document["modified"]), r"^\d{4}-\d{2}-\d{2}T")

    def test_describe_missing_file(self):
        document = _metadata.describe(str(self.dir / "nope"), "/nope")
        self.assertEqual(document, {"name": "nope", "path": "/nope"})

    def test_describe_directory_lists_entries_dirs_first(self):
        self.write("b.md", "# B\n")
        (self.dir / "sub").mkdir()
        self.write(".hidden.md", "# H\n")
        document = _metadata.describe_directory(str(self.dir), "/")
        names = [entry["name"] for entry in document["entries"]]
        self.assertEqual(names, ["sub", "b.md"])
        self.assertEqual(document["count"], 2)

    def test_describe_directory_can_include_hidden(self):
        self.write(".dot.md", "# H\n")
        document = _metadata.describe_directory(str(self.dir), "/", show_hidden=True)
        self.assertIn(".dot.md", [entry["name"] for entry in document["entries"]])

    def test_describe_directory_truncates(self):
        for index in range(5):
            self.write(f"f{index}.md", "# T\n")
        document = _metadata.describe_directory(str(self.dir), "/", limit=2)
        self.assertTrue(document["truncated"])
        self.assertEqual(document["count"], 2)

    def test_describe_directory_missing(self):
        with self.assertRaises(OSError):
            _metadata.describe_directory(str(self.dir / "nope"), "/nope/")

    def test_to_json_round_trips(self):
        path = self.write("a.md", "# T\n")
        payload = _metadata.to_json(_metadata.describe(path, "/a.md"))
        self.assertEqual(json.loads(payload)["metadata"]["title"], "T")

    def test_entry_paths_are_url_quoted(self):
        self.write("a b.md", "# T\n")
        document = _metadata.describe_directory(str(self.dir), "/")
        self.assertEqual(document["entries"][0]["path"], "/a%20b.md")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
