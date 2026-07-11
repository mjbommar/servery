"""Tests for the fast request-header parser that replaces email.feedparser."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from servery._request import (
    HeadDeadlineReader,
    HeaderBlockParser,
    HeaderError,
    RequestHeadParser,
    RequestHeadTimeoutError,
    RequestLineError,
    parse_request_line,
)
from servery.config import Config
from servery.handler import _read_request_headers, _RequestHeaders
from tests._harness import raw_exchange, serving, status_of


class RequestHeadersTest(unittest.TestCase):
    def test_case_insensitive_first_wins(self):
        h = _RequestHeaders([("Content-Type", "text/html"), ("content-type", "later")])
        self.assertEqual(h.get("CONTENT-type"), "text/html")  # first occurrence wins

    def test_get_default_and_missing(self):
        h = _RequestHeaders([("X-A", "1")])
        self.assertIsNone(h.get("missing"))
        self.assertEqual(h.get("missing", "d"), "d")
        self.assertEqual(h.get("x-a"), "1")

    def test_contains_and_getitem(self):
        h = _RequestHeaders([("X-A", "1")])
        self.assertIn("x-a", h)
        self.assertNotIn("x-b", h)
        self.assertNotIn(123, h)
        self.assertEqual(h["X-A"], "1")
        self.assertIsNone(h["nope"])  # email.Message semantics: None, not KeyError
        self.assertEqual(h.items(), [("X-A", "1")])


class ReadHeadersTest(unittest.TestCase):
    def _read(self, data: bytes) -> _RequestHeaders:
        return _read_request_headers(io.BytesIO(data))

    def test_basic_and_ows_trim(self):
        h = self._read(b"Host: example\r\nX-Pad:   spaced\t \r\n\r\n")
        self.assertEqual(h.get("Host"), "example")
        self.assertEqual(h.get("X-Pad"), "spaced")

    def test_obs_fold_merged(self):
        h = self._read(b"X-Long: a\r\n  b\r\n\tc\r\n\r\n")
        self.assertEqual(h.get("X-Long"), "a b c")

    def test_malformed_field_lines_are_rejected(self):
        for data in (
            b"Host: x\r\nGARBAGE-NO-COLON\r\n\r\n",
            b"Host : x\r\n\r\n",
            b"Bad(Name): x\r\n\r\n",
            b"X-Test: one\x00two\r\n\r\n",
            b"X-Test: one\r\r\n\r\n",
        ):
            with self.subTest(data=data), self.assertRaises(HeaderError) as raised:
                self._read(data)
            self.assertEqual(raised.exception.status, 400)

    def test_terminated_by_eof(self):
        h = self._read(b"Host: x\r\n")  # no blank line, stream ends
        self.assertEqual(h.get("Host"), "x")


class HeadDeadlineReaderTest(unittest.TestCase):
    class Source:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def peek(self, _size: int = 0) -> bytes:
            return self.data

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                size = len(self.data)
            result, self.data = self.data[:size], self.data[size:]
            return result

    class Socket:
        def __init__(self) -> None:
            self.timeout: float | None = 30.0
            self.calls: list[float | None] = []

        def gettimeout(self) -> float | None:
            return self.timeout

        def settimeout(self, value: float | None) -> None:
            self.timeout = value
            self.calls.append(value)

    def test_lines_do_not_consume_body_or_pipeline_bytes(self):
        source = self.Source(b"Host: x\r\n\r\nBODYGET /next")
        sock = self.Socket()
        with mock.patch("servery._request.time.monotonic", return_value=10.0):
            reader = HeadDeadlineReader(source, sock, 1.0, source.peek())
            self.assertEqual(reader.readline(), b"Host: x\r\n")
            self.assertEqual(reader.readline(), b"\r\n")
        self.assertEqual(source.data, b"BODYGET /next")
        self.assertEqual(sock.timeout, 30.0)

    def test_complete_buffered_head_is_consumed_in_one_exact_read(self):
        source = self.Source(b"GET / HTTP/1.1\r\nHost: x\r\n\r\nBODY")
        sock = self.Socket()
        with mock.patch("servery._request.time.monotonic", return_value=10.0):
            reader = HeadDeadlineReader(source, sock, 1.0, source.peek())
            self.assertEqual(
                reader.buffered_head(),
                b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
            )
        self.assertEqual(source.data, b"BODY")

    def test_one_deadline_spans_multiple_lines(self):
        source = self.Source(b"one\r\ntwo\r\n")
        sock = self.Socket()
        with mock.patch("servery._request.time.monotonic", side_effect=(10.0, 11.1)):
            reader = HeadDeadlineReader(source, sock, 1.0, b"one\r\n")
            self.assertEqual(reader.readline(), b"one\r\n")
            with self.assertRaises(RequestHeadTimeoutError):
                reader.readline()


class IncrementalHeadersTest(unittest.TestCase):
    def test_every_fragment_boundary_matches_blocking_parser(self):
        data = b"Host: example\r\nX-Long: a\r\n b\r\nX-End: z\r\n\r\nBODY"
        expected = _read_request_headers(io.BytesIO(data)).items()
        header_end = data.index(b"\r\n\r\n") + 4
        for split in range(len(data) + 1):
            with self.subTest(split=split):
                parser = HeaderBlockParser()
                headers, remainder = parser.feed(data[:split])
                if headers is None:
                    headers, remainder = parser.feed(data[split:])
                else:
                    remainder += data[split:]
                assert headers is not None
                self.assertEqual(headers.items(), expected)
                self.assertEqual(remainder, data[header_end:])

    def test_finish_accepts_an_unterminated_final_line(self):
        parser = HeaderBlockParser()
        headers, remainder = parser.feed(b"Host: x\r\nX-End: y")
        self.assertIsNone(headers)
        self.assertEqual(remainder, b"")
        self.assertEqual(parser.finish().items(), [("Host", "x"), ("X-End", "y")])

    def test_limits_apply_before_a_complete_line_arrives(self):
        parser = HeaderBlockParser()
        with self.assertRaisesRegex(HeaderError, "too long"):
            parser.feed(b"X: " + b"a" * 65536)

    def test_feed_after_completion_is_rejected(self):
        parser = HeaderBlockParser()
        headers, remainder = parser.feed(b"\r\nnext")
        self.assertIsNotNone(headers)
        self.assertEqual(remainder, b"next")
        with self.assertRaises(RuntimeError):
            parser.feed(b"more")


class RequestLineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "f.txt").write_text("hi there")
        self.cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_case_insensitive_header_used(self):
        # A lowercase "range:" must still trigger a 206 (header names are folded).
        with serving(self.cfg) as (host, port):
            req = b"GET /f.txt HTTP/1.1\r\nHost: x\r\nrAnGe: bytes=0-3\r\nConnection: close\r\n\r\n"
            resp = raw_exchange(host, port, req)
            self.assertEqual(status_of(resp), 206)

    def test_obs_fold_request_accepted(self):
        with serving(self.cfg) as (host, port):
            req = (
                b"GET /f.txt HTTP/1.1\r\nHost: x\r\nX-Folded: a\r\n  b\r\nConnection: close\r\n\r\n"
            )
            self.assertEqual(status_of(raw_exchange(host, port, req)), 200)

    def test_bad_version_400(self):
        # A malformed version errors before request_version is set, so the stdlib
        # emits it HTTP/0.9-style (no status line) — we match the base exactly.
        with serving(self.cfg) as (host, port):
            req = b"GET /f.txt HTTP/1.2.3\r\nHost: x\r\nConnection: close\r\n\r\n"
            resp = raw_exchange(host, port, req)
            self.assertNotIn(b"hi there", resp)  # the file is not served
            self.assertIn(b"400", resp)

    def test_http2_in_request_line_505(self):
        # A literal "HTTP/2.0" request line (not the h2 preface) is unsupported.
        with serving(self.cfg) as (host, port):
            req = b"GET /f.txt HTTP/2.0\r\nHost: x\r\nConnection: close\r\n\r\n"
            resp = raw_exchange(host, port, req)
            self.assertNotIn(b"hi there", resp)
            self.assertIn(b"505", resp)

    def test_http_0_9_request(self):
        # A 2-word "GET /path" line is HTTP/0.9: body only, no status line.
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(host, port, b"GET /f.txt\r\n\r\n")
            self.assertIn(b"hi there", resp)


class RequestLineUnitTest(unittest.TestCase):
    def test_common_versions_and_authority_collapse(self):
        one_one = parse_request_line(b"GET //example/path HTTP/1.1\r\n")
        assert one_one is not None
        self.assertEqual(one_one.target, "/example/path")
        self.assertFalse(one_one.close_connection)
        self.assertTrue(one_one.has_headers)

        one_zero = parse_request_line(b"GET / HTTP/1.0\r\n")
        assert one_zero is not None
        self.assertTrue(one_zero.close_connection)

    def test_http_0_9_is_get_only_and_has_no_headers(self):
        parsed = parse_request_line(b"GET /old\r\n")
        assert parsed is not None
        self.assertEqual(parsed.version, "HTTP/0.9")
        self.assertFalse(parsed.has_headers)
        with self.assertRaises(RequestLineError) as raised:
            parse_request_line(b"POST /old\r\n")
        self.assertEqual(raised.exception.status, 400)

    def test_errors_preserve_response_version_timing(self):
        with self.assertRaises(RequestLineError) as malformed:
            parse_request_line(b"GET / NOT-HTTP\r\n")
        self.assertIsNone(malformed.exception.response_version)

        with self.assertRaises(RequestLineError) as syntax:
            parse_request_line(b"GET / extra HTTP/1.1\r\n")
        self.assertEqual(syntax.exception.response_version, "HTTP/1.1")
        self.assertFalse(syntax.exception.close_connection)

        with self.assertRaises(RequestLineError) as unsupported:
            parse_request_line(b"GET / HTTP/2.0\r\n")
        self.assertEqual(unsupported.exception.status, 505)
        self.assertIsNone(unsupported.exception.response_version)


class RequestHeadParserTest(unittest.TestCase):
    request = (
        b"POST /submit HTTP/1.1\r\n"
        b"Host: example\r\n"
        b"Content-Length: 4\r\n"
        b"Expect: 100-continue\r\n"
        b"\r\nBODYNEXT"
    )

    def test_every_fragment_boundary_preserves_body_and_policy(self):
        boundary = self.request.index(b"\r\n\r\n") + 4
        for split in range(len(self.request) + 1):
            with self.subTest(split=split):
                parser = RequestHeadParser()
                head, remainder = parser.feed(self.request[:split])
                if head is None and not parser.complete:
                    head, remainder = parser.feed(self.request[split:])
                else:
                    remainder += self.request[split:]
                assert head is not None
                self.assertEqual(head.request.method, "POST")
                self.assertEqual(head.request.target, "/submit")
                self.assertEqual(head.body.length, 4)
                self.assertFalse(head.close_connection)
                self.assertTrue(head.expect_continue)
                self.assertEqual(remainder, self.request[boundary:])

    def test_connection_and_framing_policy(self):
        parser = RequestHeadParser()
        head, remainder = parser.feed(
            b"POST / HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: 3, 3\r\n\r\nabc"
        )
        assert head is not None
        self.assertTrue(head.close_connection)
        self.assertEqual(head.body.length, 3)
        self.assertEqual(remainder, b"abc")

        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            RequestHeadParser().feed(
                b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )

    def test_http_0_9_and_empty_line_complete_without_headers(self):
        old = RequestHeadParser()
        head, remainder = old.feed(b"GET /old\r\ntrailing")
        assert head is not None
        self.assertEqual(head.request.version, "HTTP/0.9")
        self.assertEqual(remainder, b"trailing")
        self.assertTrue(old.complete)

        empty = RequestHeadParser()
        head, remainder = empty.feed(b"\r\nnext")
        self.assertIsNone(head)
        self.assertEqual(remainder, b"next")
        self.assertTrue(empty.complete)

    def test_eof_and_limits(self):
        parser = RequestHeadParser()
        head, remainder = parser.feed(b"GET / HTTP/1.1\r\nHost: x")
        self.assertIsNone(head)
        self.assertEqual(remainder, b"")
        head = parser.finish()
        assert head is not None
        self.assertEqual(head.headers.get("Host"), "x")

        with self.assertRaisesRegex(RequestLineError, "Too Long") as raised:
            RequestHeadParser().feed(b"G" * 65537)
        self.assertEqual(raised.exception.status, 414)

    def test_body_limit_chunked_and_post_completion(self):
        with self.assertRaisesRegex(ValueError, "size limit"):
            RequestHeadParser(max_body_size=3).feed(
                b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\n"
            )

        parser = RequestHeadParser(allow_chunked=True)
        head, _ = parser.feed(b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n")
        assert head is not None
        self.assertTrue(head.body.chunked)
        with self.assertRaises(RuntimeError):
            parser.feed(b"more")

    def test_host_policy(self):
        valid = (
            b"GET / HTTP/1.1\r\nHost: example.test:8080\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: [::1]:8080\r\n\r\n",
            b"GET / HTTP/1.0\r\n\r\n",
        )
        for request in valid:
            with self.subTest(request=request):
                head, _ = RequestHeadParser().feed(request)
                self.assertIsNotNone(head)

        invalid = (
            b"GET / HTTP/1.1\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: one\r\nHost: two\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost:\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: user@example.test\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: example.test:notaport\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: ::1\r\n\r\n",
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, "Host header"):
                RequestHeadParser().feed(request)


class HostAndFieldWireTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "f.txt").write_text("host-safe")
        self.cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_invalid_http_1_1_host_forms_are_400(self):
        requests = (
            b"GET /f.txt HTTP/1.1\r\n\r\n",
            b"GET /f.txt HTTP/1.1\r\nHost: one\r\nHost: two\r\n\r\n",
            b"GET /f.txt HTTP/1.1\r\nHost: bad host\r\n\r\n",
        )
        with serving(self.cfg) as (host, port):
            for request in requests:
                with self.subTest(request=request):
                    response = raw_exchange(host, port, request)
                    self.assertEqual(status_of(response), 400)
                    self.assertNotIn(b"host-safe", response)

    def test_malformed_field_line_is_400_and_connection_closes(self):
        request = (
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nBad Field: value\r\n\r\n"
            b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        with serving(self.cfg) as (host, port):
            response = raw_exchange(host, port, request)
        self.assertEqual(status_of(response), 400)
        self.assertNotIn(b"host-safe", response)


if __name__ == "__main__":
    unittest.main()
