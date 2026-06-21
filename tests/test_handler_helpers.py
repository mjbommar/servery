"""Unit tests for handler-level conditional-request helpers."""

import io
import tempfile
import unittest
from pathlib import Path

from servery.handler import (
    _content_disposition,
    _copy_n,
    _etag_matches,
    _make_etag,
    _not_modified_since,
)


class EtagMatchTest(unittest.TestCase):
    def test_star_matches_anything(self):
        self.assertTrue(_etag_matches("*", '"abc"'))

    def test_weak_and_strong_compare_equal(self):
        self.assertTrue(_etag_matches('W/"abc"', '"abc"'))
        self.assertTrue(_etag_matches('"abc"', '"abc"'))

    def test_list_membership(self):
        self.assertTrue(_etag_matches('"x", "abc"', '"abc"'))
        self.assertFalse(_etag_matches('"x"', '"abc"'))


class NotModifiedSinceTest(unittest.TestCase):
    def test_bad_date_is_false(self):
        self.assertFalse(_not_modified_since("not a date", 0.0))

    def test_future_date_is_not_modified(self):
        self.assertTrue(_not_modified_since("Wed, 21 Oct 2099 07:28:00 GMT", 0.0))


class CopyNTest(unittest.TestCase):
    def test_copies_exact_count_from_offset(self):
        source = io.BytesIO(b"abcdefgh")
        source.seek(2)
        dest = io.BytesIO()
        _copy_n(source, dest, 3)
        self.assertEqual(dest.getvalue(), b"cde")

    def test_stops_at_eof(self):
        source = io.BytesIO(b"ab")
        dest = io.BytesIO()
        _copy_n(source, dest, 100)
        self.assertEqual(dest.getvalue(), b"ab")


class ContentDispositionTest(unittest.TestCase):
    def test_strips_crlf_to_prevent_header_injection(self):
        value = _content_disposition("ev\r\nX-Injected: pwned.zip")
        self.assertNotIn("\r", value)
        self.assertNotIn("\n", value)

    def test_normal_filename(self):
        value = _content_disposition("photo.zip")
        self.assertIn('filename="photo.zip"', value)
        self.assertIn("filename*=UTF-8''photo.zip", value)


class MakeEtagTest(unittest.TestCase):
    def test_format(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"hi")
            handle.flush()
            etag = _make_etag(Path(handle.name).stat())
        self.assertTrue(etag.startswith('"'))
        self.assertTrue(etag.endswith('"'))
        self.assertIn("-", etag)


if __name__ == "__main__":
    unittest.main()
