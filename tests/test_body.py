"""Shared HTTP request-framing and keep-alive disposal tests."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from servery import _body
from servery.config import Config
from tests._harness import raw_exchange, serving, status_of


class ParseFramingTest(unittest.TestCase):
    def test_absent_framing_is_an_empty_body(self):
        self.assertEqual(_body.parse_framing([], []), _body.BodyPlan(0))

    def test_identical_duplicate_lengths_are_normalized(self):
        self.assertEqual(_body.parse_framing(["4", "4"], []), _body.BodyPlan(4))
        self.assertEqual(_body.parse_framing(["4, 4"], []), _body.BodyPlan(4))

    def test_conflicting_or_invalid_lengths_are_rejected(self):
        for values in (["4", "5"], ["-1"], ["x"], [""], ["\N{FULLWIDTH DIGIT FOUR}"]):
            with self.subTest(values=values), self.assertRaises(_body.FramingError):
                _body.parse_framing(values, [])

    def test_transfer_encoding_and_length_is_always_ambiguous(self):
        with self.assertRaises(_body.FramingError):
            _body.parse_framing(["4"], ["chunked"], allow_chunked=True)

    def test_chunked_is_adapter_policy_and_size_is_configurable(self):
        with self.assertRaises(_body.FramingError) as chunked:
            _body.parse_framing([], ["chunked"])
        self.assertEqual(chunked.exception.status, 501)
        self.assertTrue(_body.parse_framing([], ["chunked"], allow_chunked=True).chunked)
        with self.assertRaises(_body.FramingError) as oversized:
            _body.parse_framing(["11"], [], max_size=10)
        self.assertEqual(oversized.exception.status, 413)

    def test_empty_or_non_chunked_transfer_coding_is_rejected(self):
        for values, status in (([""], 400), (["gzip"], 501), (["gzip, chunked"], 501)):
            with self.subTest(values=values), self.assertRaises(_body.FramingError) as error:
                _body.parse_framing([], values, allow_chunked=True)
            self.assertEqual(error.exception.status, status)


class LimitedReaderTest(unittest.TestCase):
    def test_reads_never_cross_the_declared_boundary(self):
        source = io.BytesIO(b"bodyNEXT")
        reader = _body.LimitedReader(source, 4)
        self.assertEqual(reader.read(), b"body")
        self.assertEqual(reader.read(), b"")
        self.assertEqual(source.read(), b"NEXT")

    def test_drain_limit_preserves_small_bodies_and_refuses_large_ones(self):
        small = _body.LimitedReader(io.BytesIO(b"abcd"), 4)
        self.assertTrue(small.drain(4))
        large = _body.LimitedReader(io.BytesIO(b"abcde"), 5)
        self.assertFalse(large.drain(4))
        self.assertEqual(large.remaining, 5)

    def test_line_iteration_is_bounded(self):
        reader = _body.LimitedReader(io.BytesIO(b"a\nb\nNEXT"), 4)
        self.assertEqual(reader.readline(), b"a\n")
        self.assertEqual(reader.readlines(), [b"b\n"])
        self.assertEqual(reader.readline(), b"")
        self.assertEqual(reader.remaining, 0)

        iterable = _body.LimitedReader(io.BytesIO(b"c\nd\n"), 4)
        self.assertEqual(list(iterable), [b"c\n", b"d\n"])

    def test_line_fallback_and_short_source(self):
        class ReadOnly:
            def __init__(self, data: bytes) -> None:
                self.source = io.BytesIO(data)

            def read(self, size: int) -> bytes:
                return self.source.read(size)

        reader = _body.LimitedReader(ReadOnly(b"ab"), 2)
        self.assertEqual(reader.readline(1), b"a")
        self.assertEqual(reader.readline(), b"b")

        short = _body.LimitedReader(io.BytesIO(b"x"), 2)
        self.assertFalse(short.drain())
        self.assertEqual(short.remaining, 1)


class FramingWireTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "f.txt").write_text("ok")
        self.cfg = Config.create(self.root, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _exchange(self, request: bytes) -> bytes:
        with serving(self.cfg) as (host, port):
            return raw_exchange(host, port, request)

    def test_conflicting_duplicate_length_cannot_reach_follow_on_request(self):
        response = self._exchange(
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n"
            b"Content-Length: 2\r\n\r\nX"
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual(status_of(response), 400)
        self.assertNotIn(b"200 OK", response)

    def test_transfer_encoding_plus_length_cannot_reach_follow_on_request(self):
        response = self._exchange(
            b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
            b"Content-Length: 4\r\n\r\n0\r\n\r\n"
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual(status_of(response), 400)
        self.assertNotIn(b"200 OK", response)

    def test_unconsumed_get_body_forces_close_before_follow_on_request(self):
        response = self._exchange(
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\nJUNK"
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 1)

    def test_identical_zero_lengths_preserve_keep_alive(self):
        response = self._exchange(
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
            b"Content-Length: 0\r\n\r\n"
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 2)


if __name__ == "__main__":
    unittest.main()
