"""HTTP/1.1 correctness/conformance tests (RFC 9110/9112) + httpx interop.

Raw sockets are used where exact bytes matter (methods, path-traversal vectors);
httpx is used as an independent client to cross-check real-world behavior.
"""

from __future__ import annotations

import http.client
import tempfile
import unittest
from pathlib import Path

from servery.config import Config
from tests._harness import body_of, get_raw, raw_exchange, serving, status_of

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


def _make_tree() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "hello.txt").write_text("hi there")
    (root / "empty.txt").write_text("")
    (root / "page.html").write_text("<h1>hi</h1>")
    (root / "data.json").write_text('{"a":1}')
    (root / "style.css").write_text("body{}")
    (root / "blob.unknownext").write_bytes(b"\x00\x01\x02")
    (root / "sub").mkdir()
    return tmp


class MethodTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _make_tree()
        self._cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unsupported_methods_501(self):
        with serving(self._cfg) as (host, port):
            for method in ("PUT", "DELETE", "PATCH"):
                request = f"{method} /hello.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                resp = raw_exchange(host, port, request.encode())
                self.assertEqual(status_of(resp), 501, method)

    def test_options_returns_204(self):
        with serving(self._cfg) as (host, port):
            request = b"OPTIONS / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            self.assertEqual(status_of(raw_exchange(host, port, request)), 204)


class PathTraversalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _make_tree()
        # A secret OUTSIDE the served root.
        self._secret = Path(self._tmp.name).parent / "servery_traversal_secret.txt"
        self._secret.write_text("TRAVERSAL_LEAK")
        self._cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._secret.unlink(missing_ok=True)
        self._tmp.cleanup()

    def test_traversal_vectors_never_leak(self):
        name = self._secret.name
        vectors = [
            f"/../{name}",
            f"/%2e%2e/{name}",
            f"/..%2f{name}",
            f"/%2e%2e%2f{name}",
            f"/....//{name}",
            f"/..\\{name}",
            f"//../{name}",
            f"/sub/../../{name}",
            f"/%2e%2e/%2e%2e/{name}",
        ]
        with serving(self._cfg) as (host, port):
            for target in vectors:
                resp = get_raw(host, port, target)
                self.assertNotIn(b"TRAVERSAL_LEAK", body_of(resp), target)
                self.assertNotEqual(status_of(resp), 200, target)


class ConditionalPrecedenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _make_tree()
        self._cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _conn(self, host, port):
        return http.client.HTTPConnection(host, port, timeout=5)

    def test_if_none_match_takes_precedence_over_if_modified_since(self):
        with serving(self._cfg) as (host, port):
            conn = self._conn(host, port)
            conn.request("GET", "/hello.txt")
            first = conn.getresponse()
            first.read()
            etag = first.getheader("ETag")
            assert etag is not None
            # Non-matching If-None-Match + a future If-Modified-Since: INM is
            # authoritative (no match) -> 200, NOT the 304 that IMS alone would give.
            conn.request(
                "GET",
                "/hello.txt",
                headers={
                    "If-None-Match": '"does-not-match"',
                    "If-Modified-Since": "Wed, 21 Oct 2099 07:28:00 GMT",
                },
            )
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 200)

    def test_if_none_match_star(self):
        with serving(self._cfg) as (host, port):
            conn = self._conn(host, port)
            conn.request("GET", "/hello.txt", headers={"If-None-Match": "*"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 304)


class RepresentationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _make_tree()
        self._cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _head_get(self, host, port, path):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", path)
        get = conn.getresponse()
        get_body = get.read()
        get_headers = dict(get.getheaders())
        conn.request("HEAD", path)
        head = conn.getresponse()
        head_body = head.read()
        head_headers = dict(head.getheaders())
        conn.close()
        return (get, get_body, get_headers), (head, head_body, head_headers)

    def test_head_matches_get_headers_and_has_no_body(self):
        with serving(self._cfg) as (host, port):
            (get, _, gh), (head, hbody, hh) = self._head_get(host, port, "/hello.txt")
            self.assertEqual(get.status, 200)
            self.assertEqual(head.status, 200)
            self.assertEqual(hbody, b"")
            for header in ("Content-Type", "Content-Length", "ETag", "Last-Modified"):
                self.assertEqual(gh.get(header), hh.get(header), header)

    def test_mime_types(self):
        cases = {
            "/page.html": "text/html",
            "/data.json": "application/json",
            "/style.css": "text/css",
            "/blob.unknownext": "application/octet-stream",
        }
        with serving(self._cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            for path, expected in cases.items():
                conn.request("GET", path)
                resp = conn.getresponse()
                resp.read()
                self.assertIn(expected, resp.getheader("Content-Type", ""), path)
            conn.close()

    def test_empty_file(self):
        with serving(self._cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/empty.txt")
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Length"), "0")
            self.assertEqual(body, b"")
            # A range on an empty representation is unsatisfiable.
            conn.request("GET", "/empty.txt", headers={"Range": "bytes=0-10"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 416)

    def test_multi_range_served_full(self):
        # We serve a multi-range request as a full 200 (a permitted MAY).
        with serving(self._cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/hello.txt", headers={"Range": "bytes=0-1,3-4"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertEqual(body, b"hi there")


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class HttpxInteropTest(unittest.TestCase):
    def setUp(self):
        self._tmp = _make_tree()

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self, **kw):
        return Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True, **kw)

    def test_get_and_range_and_listing(self):
        with serving(self._cfg()) as (host, port):
            base = f"http://{host}:{port}"
            with httpx.Client() as client:
                self.assertEqual(client.get(f"{base}/hello.txt").text, "hi there")
                ranged = client.get(f"{base}/hello.txt", headers={"Range": "bytes=0-3"})
                self.assertEqual(ranged.status_code, 206)
                self.assertEqual(ranged.content, b"hi t")
                self.assertEqual(ranged.headers["content-range"], "bytes 0-3/8")
                self.assertIn("hello.txt", client.get(f"{base}/").text)

    def test_conditional_304(self):
        with serving(self._cfg()) as (host, port):
            url = f"http://{host}:{port}/hello.txt"
            with httpx.Client() as client:
                etag = client.get(url).headers["etag"]
                again = client.get(url, headers={"If-None-Match": etag})
                self.assertEqual(again.status_code, 304)

    def test_auth(self):
        with serving(self._cfg(auth="alice:secret")) as (host, port):
            url = f"http://{host}:{port}/hello.txt"
            with httpx.Client() as client:
                self.assertEqual(client.get(url).status_code, 401)
                ok = client.get(url, auth=httpx.BasicAuth("alice", "secret"))
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(ok.text, "hi there")


if __name__ == "__main__":
    unittest.main()
