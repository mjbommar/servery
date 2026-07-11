"""Unit tests for handler-level conditional-request helpers."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from servery._conditional import etag_matches as _etag_matches
from servery._conditional import make_etag as _make_etag
from servery._conditional import not_modified_since as _not_modified_since
from servery.handler import ServeryHandler, _content_disposition, _copy_n


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


class _RecordingSocket:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.sendfile_calls: list[tuple[int, int]] = []

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def sendfile(self, source: io.BytesIO, offset: int, count: int) -> None:
        self.sendfile_calls.append((offset, count))
        source.seek(offset)
        self.sendall(source.read(count))


class StaticBodyStrategyTest(unittest.TestCase):
    def _handler(self, threshold: int, *, count: int, offset: int = 0) -> Any:
        handler = cast(Any, object.__new__(ServeryHandler))
        handler.connection = _RecordingSocket()
        handler.server = SimpleNamespace(
            config=SimpleNamespace(small_file_buffer_size=threshold, write_timeout=None)
        )
        handler._body_remaining = count
        handler._body_offset = offset
        return handler

    def test_small_plaintext_body_uses_one_bounded_send(self) -> None:
        handler = self._handler(4, count=3, offset=2)
        handler._send_body(io.BytesIO(b"abcdef"))
        self.assertEqual(handler.connection.sent, b"cde")
        self.assertEqual(handler.connection.sendfile_calls, [])

    @unittest.skipUnless(hasattr(os, "sendfile"), "native sendfile is Unix-only")
    def test_zero_or_smaller_threshold_retains_sendfile(self) -> None:
        for threshold in (0, 2):
            with self.subTest(threshold=threshold):
                handler = self._handler(threshold, count=3, offset=1)
                handler._send_body(io.BytesIO(b"abcdef"))
                self.assertEqual(handler.connection.sent, b"bcd")
                self.assertEqual(handler.connection.sendfile_calls, [(1, 3)])


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
