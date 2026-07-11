"""Differential contracts for HTTP/1 and the shared HTTP/2/3 static planner."""

from __future__ import annotations

import contextlib
import gzip
import http.client
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path

from servery import _response, _static
from servery.config import Config
from servery.server import make_server

_SHARED_FILE_HEADERS = (
    "content-type",
    "content-length",
    "etag",
    "last-modified",
    "cache-control",
    "x-content-type-options",
    "access-control-allow-origin",
)


@contextlib.contextmanager
def _serving(config: Config) -> Iterator[tuple[str, int]]:
    server = make_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield str(server.server_address[0]), int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _headers_dict(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {name.decode("latin-1"): value.decode("latin-1") for name, value in headers}


def _planned_body(body: _response.ResponseBody) -> bytes:
    if isinstance(body, bytes):
        return body
    try:
        return body.handle.read(body.size)
    finally:
        body.close()


class StaticResponseContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "hello.txt").write_text("hi there")
        (self.root / "empty.bin").write_bytes(b"")
        (self.root / "data.json").write_text('{"ok":true}')
        (self.root / "large.txt").write_text("compress me " * 1000)
        (self.root / "sub").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, *, compress: bool) -> Config:
        return Config.create(
            self.root,
            host="127.0.0.1",
            port=0,
            quiet=True,
            cors=True,
            cache_max_age=60,
            compress=compress,
        )

    def test_regular_file_get_and_head_share_logical_headers(self) -> None:
        config = self._config(compress=False)
        with _serving(config) as (host, port):
            connection = http.client.HTTPConnection(host, port, timeout=5)
            for target in ("/hello.txt", "/empty.bin", "/data.json"):
                with self.subTest(target=target):
                    fs_path = str(self.root / target.removeprefix("/"))
                    status, planned_headers, planned_body = _response.build_static(
                        config, fs_path, target, "", tls=False
                    )
                    expected_headers = _headers_dict(planned_headers)
                    expected_body = _planned_body(planned_body)
                    for method in ("GET", "HEAD"):
                        connection.request(method, target)
                        response = connection.getresponse()
                        actual_body = response.read()
                        actual_headers = {
                            name.lower(): value for name, value in response.getheaders()
                        }
                        self.assertEqual(response.status, status)
                        for name in _SHARED_FILE_HEADERS:
                            self.assertEqual(
                                actual_headers.get(name), expected_headers.get(name), name
                            )
                        self.assertEqual(actual_body, expected_body if method == "GET" else b"")
            connection.close()

    def test_conditional_status_and_validators_match(self) -> None:
        config = self._config(compress=False)
        path = str(self.root / "hello.txt")
        _status, headers, body = _response.build_static(config, path, "/hello.txt", "", tls=False)
        _planned_body(body)
        etag = _headers_dict(headers)["etag"]

        planned_status, planned_headers, planned_body = _response.build_static(
            config, path, "/hello.txt", "", tls=False, if_none_match=etag
        )
        self.assertEqual(_planned_body(planned_body), b"")
        expected = _headers_dict(planned_headers)
        with _serving(config) as (host, port):
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/hello.txt", headers={"If-None-Match": etag})
            response = connection.getresponse()
            self.assertEqual(response.read(), b"")
            actual = {name.lower(): value for name, value in response.getheaders()}
            connection.close()

        self.assertEqual(response.status, planned_status)
        for name in ("etag", "last-modified", "x-content-type-options"):
            self.assertEqual(actual.get(name), expected.get(name), name)

    def test_identity_selection_covers_conditional_and_range_ladder(self) -> None:
        path = str(self.root / "hello.txt")
        opened = _static.open_file(
            path,
            "text/plain; charset=utf-8",
            "",
            compression_enabled=False,
            max_compress_size=0,
        )
        try:
            common = {
                "if_range": None,
                "if_none_match": None,
                "if_modified_since": None,
            }
            full = _static.select_identity(opened, range_header=None, **common)
            partial = _static.select_identity(opened, range_header="bytes=1-3", **common)
            suffix = _static.select_identity(opened, range_header="bytes=-2", **common)
            unsatisfiable = _static.select_identity(
                opened,
                range_header="bytes=99-100",
                **common,
            )
            changed = _static.select_identity(
                opened,
                range_header="bytes=1-3",
                if_range='"changed"',
                if_none_match=None,
                if_modified_since=None,
            )
            not_modified = _static.select_identity(
                opened,
                range_header="bytes=1-3",
                if_range=None,
                if_none_match=opened.etag,
                if_modified_since=None,
            )
        finally:
            opened.close()

        self.assertEqual((full.status, full.offset, full.count), (200, 0, 8))
        self.assertEqual((partial.status, partial.offset, partial.count), (206, 1, 3))
        self.assertEqual(partial.content_range, "bytes 1-3/8")
        self.assertEqual((suffix.status, suffix.offset, suffix.count), (206, 6, 2))
        self.assertEqual(unsatisfiable.status, 416)
        self.assertEqual(unsatisfiable.content_range, "bytes */8")
        self.assertEqual((changed.status, changed.offset, changed.count), (200, 0, 8))
        self.assertEqual(not_modified.status, 304)

    def test_directory_redirect_and_contained_index_primitives(self) -> None:
        indexed = self.root / "indexed"
        indexed.mkdir()
        index = indexed / "index.html"
        index.write_text("index")

        self.assertEqual(_static.directory_redirect("/indexed?view=1"), "/indexed/?view=1")
        self.assertIsNone(_static.directory_redirect("/indexed/?view=1"))
        self.assertIsNone(_static.directory_redirect("/indexed%2F?view=1"))
        self.assertEqual(
            _static.find_contained_index(
                str(self.root.resolve()),
                str(indexed),
                ("missing.html", "index.html"),
            ),
            str(index),
        )
        self.assertIsNone(
            _static.find_contained_index(
                str(self.root.resolve()),
                str(self.root / "sub"),
                ("index.html", "index.htm"),
            )
        )

    def test_download_request_and_disposition_are_shared_and_injection_safe(self) -> None:
        self.assertTrue(_static.download_requested("/hello.txt?download=1"))
        self.assertTrue(_static.download_requested("/hello.txt?x=1&download=0"))
        self.assertFalse(_static.download_requested("/download/file.txt"))
        self.assertFalse(_static.download_requested("/hello.txt?download="))

        disposition = _static.content_disposition('résumé\r\nX-Evil: yes".txt')
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)
        self.assertIn('filename="r?sum?X-Evil: yes.txt"', disposition)
        self.assertIn("filename*=UTF-8''r%C3%A9sum%C3%A9%0D%0AX-Evil%3A%20yes%22.txt", disposition)

    def test_compressed_representation_agrees(self) -> None:
        config = self._config(compress=True)
        path = str(self.root / "large.txt")
        status, headers, body = _response.build_static(
            config, path, "/large.txt", "gzip", tls=False
        )
        expected_headers = _headers_dict(headers)
        expected_body = _planned_body(body)

        with _serving(config) as (host, port):
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/large.txt", headers={"Accept-Encoding": "gzip"})
            response = connection.getresponse()
            actual_body = response.read()
            actual_headers = {name.lower(): value for name, value in response.getheaders()}
            connection.close()

        self.assertEqual(response.status, status)
        for name in (*_SHARED_FILE_HEADERS, "vary", "content-encoding"):
            actual_value = actual_headers.get(name)
            expected_value = expected_headers.get(name)
            if name == "vary":
                actual_value = actual_value.lower() if actual_value is not None else None
                expected_value = expected_value.lower() if expected_value is not None else None
            self.assertEqual(actual_value, expected_value, name)
        self.assertEqual(gzip.decompress(actual_body), gzip.decompress(expected_body))

    def test_directory_redirect_status_and_location_match(self) -> None:
        config = self._config(compress=False)
        planned_status, headers, body = _response.build_static(
            config, str(self.root / "sub"), "/sub", "", tls=False
        )
        self.assertEqual(_planned_body(body), b"")
        expected_location = _headers_dict(headers)["location"]

        with _serving(config) as (host, port):
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/sub")
            response = connection.getresponse()
            self.assertEqual(response.read(), b"")
            connection.close()

        self.assertEqual(response.status, planned_status)
        self.assertEqual(response.getheader("Location"), expected_location)

    def test_missing_or_escaped_resource_status_matches(self) -> None:
        config = self._config(compress=False)
        for target, fs_path in (("/missing", str(self.root / "missing")), ("/escape", "")):
            with self.subTest(target=target):
                planned_status, _headers, body = _response.build_static(
                    config, fs_path, target, "", tls=False
                )
                _planned_body(body)
                with _serving(config) as (host, port):
                    connection = http.client.HTTPConnection(host, port, timeout=5)
                    connection.request("GET", target)
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                self.assertEqual(response.status, planned_status)


if __name__ == "__main__":
    unittest.main()
