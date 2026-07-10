"""WebDAV (RFC 4918) tests: the mount-critical methods + Destination containment."""

from __future__ import annotations

import http.client
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from servery import _webdav
from servery.config import Config
from tests._harness import serving


class _DavCase(unittest.TestCase):
    dav = True
    dav_write = True
    allow_overwrite = False
    dav_lock_mode = "enforced"
    max_propfind_entries = 10_000

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "hello.txt").write_text("hi")
        (self.root / "sub").mkdir()
        self.cfg = Config.create(
            str(self.root),
            host="127.0.0.1",
            port=0,
            quiet=True,
            dav=self.dav,
            dav_write=self.dav_write,
            allow_overwrite=self.allow_overwrite,
            dav_lock_mode=self.dav_lock_mode,
            max_propfind_entries=self.max_propfind_entries,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _req(self, method, path, body=None, headers=None):
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            status, hdrs = resp.status, dict(resp.getheaders())
            conn.close()
            return status, hdrs, data


class DavMethodTest(_DavCase):
    def test_options_advertises_class_2(self):
        status, hdrs, _ = self._req("OPTIONS", "/")
        self.assertEqual(status, 204)
        self.assertEqual(hdrs.get("DAV"), "1, 2")  # class 2 -> clients mount read-write
        self.assertEqual(hdrs.get("MS-Author-Via"), "DAV")
        self.assertIn("PUT", hdrs.get("Allow", ""))

    def test_propfind_depth_1(self):
        status, hdrs, body = self._req("PROPFIND", "/", headers={"Depth": "1"})
        self.assertEqual(status, 207)
        self.assertIn("xml", hdrs.get("Content-Type", ""))
        self.assertIn(b"hello.txt", body)
        self.assertIn(b"collection", body)  # the root resourcetype
        self.assertIn(b"getlastmodified", body)
        self.assertIn(b"text/plain", body)  # real MIME type, not a hardcoded octet-stream

    def test_propfind_infinity_is_bounded(self):
        status, _, body = self._req("PROPFIND", "/", headers={"Depth": "infinity"})
        self.assertEqual(status, 403)
        self.assertIn(b"propfind-finite-depth", body)

    def test_put_then_get(self):
        self.assertEqual(self._req("PUT", "/new.txt", body=b"data")[0], 201)
        self.assertEqual(self._req("GET", "/new.txt")[2], b"data")

    def test_put_missing_parent_is_409(self):
        self.assertEqual(self._req("PUT", "/nope/x.txt", body=b"x")[0], 409)

    def test_mkcol(self):
        self.assertEqual(self._req("MKCOL", "/dir")[0], 201)
        self.assertTrue((self.root / "dir").is_dir())
        self.assertEqual(self._req("MKCOL", "/sub")[0], 405)  # already exists
        self.assertEqual(self._req("MKCOL", "/a/b")[0], 409)  # missing parent

    def test_delete_file_and_collection(self):
        self.assertEqual(self._req("DELETE", "/hello.txt")[0], 204)
        self.assertFalse((self.root / "hello.txt").exists())
        self.assertEqual(self._req("DELETE", "/sub")[0], 204)
        self.assertFalse((self.root / "sub").exists())

    def test_move(self):
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "MOVE", "/hello.txt", headers={"Destination": f"http://{host}:{port}/sub/m.txt"}
            )
            self.assertEqual(conn.getresponse().status, 201)
            conn.close()
        self.assertEqual((self.root / "sub" / "m.txt").read_text(), "hi")
        self.assertFalse((self.root / "hello.txt").exists())

    def test_copy(self):
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "COPY", "/hello.txt", headers={"Destination": f"http://{host}:{port}/c.txt"}
            )
            self.assertEqual(conn.getresponse().status, 201)
            conn.close()
        self.assertEqual((self.root / "c.txt").read_text(), "hi")
        self.assertTrue((self.root / "hello.txt").exists())  # original kept (copy, not move)

    def test_copy_overwrite_false_is_412(self):
        (self.root / "dest.txt").write_text("old")
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "COPY",
                "/hello.txt",
                headers={"Destination": f"http://{host}:{port}/dest.txt", "Overwrite": "F"},
            )
            self.assertEqual(conn.getresponse().status, 412)
            conn.close()
        self.assertEqual((self.root / "dest.txt").read_text(), "old")  # untouched

    def test_proppatch_accepts(self):
        status, _, body = self._req(
            "PROPPATCH",
            "/hello.txt",
            body=b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
            b"<D:foo>1</D:foo></D:prop></D:set></D:propertyupdate>",
        )
        self.assertEqual(status, 207)
        self.assertIn(b"200 OK", body)

    def test_propfind_missing_is_404(self):
        self.assertEqual(self._req("PROPFIND", "/nope", headers={"Depth": "0"})[0], 404)

    def test_put_on_collection_is_405(self):
        self.assertEqual(self._req("PUT", "/sub", body=b"x")[0], 405)

    def test_delete_missing_is_404(self):
        self.assertEqual(self._req("DELETE", "/nope")[0], 404)

    def test_mkcol_with_body_is_415(self):
        self.assertEqual(self._req("MKCOL", "/d", body=b"junk")[0], 415)

    def test_move_missing_source_is_404(self):
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("MOVE", "/nope", headers={"Destination": f"http://{host}:{port}/x"})
            self.assertEqual(conn.getresponse().status, 404)
            conn.close()

    def test_move_without_destination_is_400(self):
        self.assertEqual(self._req("MOVE", "/hello.txt")[0], 400)

    def test_proppatch_missing_is_404(self):
        self.assertEqual(self._req("PROPPATCH", "/nope", body=b"<x/>")[0], 404)

    def test_copy_to_missing_parent_is_409(self):
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "COPY", "/hello.txt", headers={"Destination": f"http://{host}:{port}/no/x"}
            )
            self.assertEqual(conn.getresponse().status, 409)
            conn.close()

    def test_move_onto_itself_is_403(self):
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "MOVE", "/hello.txt", headers={"Destination": f"http://{host}:{port}/hello.txt"}
            )
            self.assertEqual(conn.getresponse().status, 403)
            conn.close()

    def test_lock_returns_token(self):
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            body = (
                b'<D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
                b"<D:locktype><D:write/></D:locktype></D:lockinfo>"
            )
            conn.request("LOCK", "/hello.txt", body=body)
            response = conn.getresponse()
            payload = response.read()
            token = response.getheader("Lock-Token")
            self.assertEqual(response.status, 200)
            self.assertTrue((token or "").startswith("<opaquelocktoken:"))
            self.assertIn(b"activelock", payload)
            conn.request("UNLOCK", "/hello.txt", headers={"Lock-Token": token or ""})
            self.assertEqual(conn.getresponse().status, 204)
            conn.close()


class DavSecurityTest(_DavCase):
    def test_destination_cannot_escape_root(self):
        # A Destination trying to climb out of the root must never write outside it.
        # safe_join neutralizes the "..", so the move lands INSIDE root (here at
        # root/escape.txt) rather than at the parent — the file never escapes.
        outside = self.root.parent / "escape.txt"
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request(
                "MOVE", "/hello.txt", headers={"Destination": f"http://{host}:{port}/../escape.txt"}
            )
            conn.getresponse().read()
            conn.close()
        self.assertFalse(outside.exists())  # the security guarantee: no escape

    def test_overwrite_policy(self):
        # allow_overwrite is off -> PUT over an existing file is refused (412).
        self.assertEqual(self._req("PUT", "/hello.txt", body=b"new")[0], 412)
        self.assertEqual((self.root / "hello.txt").read_text(), "hi")


class DavEnforcedLockTest(_DavCase):
    allow_overwrite = True

    @staticmethod
    def _request(host, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            conn.close()

    def test_depth_infinity_lock_blocks_descendant_write_without_token(self):
        lock_body = (
            b'<D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
            b"<D:locktype><D:write/></D:locktype></D:lockinfo>"
        )
        with serving(self.cfg) as (host, port):
            status, headers, _ = self._request(host, port, "LOCK", "/", lock_body)
            self.assertEqual(status, 200)
            token = headers["Lock-Token"]
            self.assertEqual(self._request(host, port, "PUT", "/new.txt", b"no token")[0], 423)
            self.assertEqual(
                self._request(host, port, "PUT", "/new.txt", b"authorized", {"If": f"({token})"})[
                    0
                ],
                201,
            )
        self.assertEqual((self.root / "new.txt").read_bytes(), b"authorized")

    def test_descendant_lock_blocks_parent_delete(self):
        (self.root / "sub" / "child.txt").write_text("x")
        body = b'<D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope></D:lockinfo>'
        with serving(self.cfg) as (host, port):
            status, _headers, _ = self._request(host, port, "LOCK", "/sub/child.txt", body)
            self.assertEqual(status, 200)
            self.assertEqual(self._request(host, port, "DELETE", "/sub")[0], 423)
        self.assertTrue((self.root / "sub" / "child.txt").exists())

    def test_empty_lock_body_refreshes_submitted_token(self):
        body = b'<D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope></D:lockinfo>'
        with serving(self.cfg) as (host, port):
            status, headers, _ = self._request(host, port, "LOCK", "/hello.txt", body)
            self.assertEqual(status, 200)
            token = headers["Lock-Token"]
            refreshed, refreshed_headers, _ = self._request(
                host, port, "LOCK", "/hello.txt", b"", {"If": f"({token})"}
            )
            self.assertEqual(refreshed, 200)
            self.assertEqual(refreshed_headers["Lock-Token"], token)


class DavClass1ModeTest(_DavCase):
    dav_lock_mode = "class1"

    def test_advertising_and_methods_are_honest(self):
        status, headers, _ = self._req("OPTIONS", "/")
        self.assertEqual(status, 204)
        self.assertEqual(headers["DAV"], "1")
        self.assertNotIn("LOCK", headers["Allow"])
        self.assertEqual(self._req("LOCK", "/hello.txt", body=b"")[0], 405)


class DavCompatModeTest(_DavCase):
    dav_lock_mode = "compat"

    def test_fake_token_does_not_enforce_and_warns(self):
        self.assertTrue(
            any("without enforcing" in warning for warning in self.cfg.startup_warnings())
        )
        status, headers, _ = self._req("LOCK", "/hello.txt", body=b"lock")
        self.assertEqual(status, 200)
        self.assertIn("opaquelocktoken", headers["Lock-Token"])
        self.assertEqual(self._req("DELETE", "/hello.txt")[0], 204)


class DavPropfindBudgetTest(_DavCase):
    max_propfind_entries = 1

    def test_depth_one_over_limit_is_explicit_507(self):
        (self.root / "second.txt").write_text("2")
        status, _, body = self._req("PROPFIND", "/", headers={"Depth": "1"})
        self.assertEqual(status, 507)
        self.assertNotIn(b"207 Multi-Status", body)


class DavLockManagerTest(unittest.TestCase):
    def test_expired_lock_is_purged(self):
        manager = _webdav.DavLockManager()
        with mock.patch.object(_webdav.time, "time", return_value=100.0):
            record = manager.acquire("/tmp/x", 5)
        self.assertIsNotNone(record)
        with mock.patch.object(_webdav.time, "time", return_value=106.0):
            self.assertTrue(manager.authorized(["/tmp/x"], ""))

    def test_conflict_refresh_release_and_authorization_paths(self):
        manager = _webdav.DavLockManager()
        record = manager.acquire("/tmp/root", 60, "owner")
        if record is None:
            self.fail("initial lock was not acquired")
        self.assertIsNone(manager.acquire("/tmp/root/child", 60))
        self.assertFalse(manager.authorized(["/tmp/root/child"], "wrong"))
        self.assertTrue(manager.authorized(["/tmp/root/child"], record.token))
        self.assertEqual(manager.discover("/tmp/root/child"), [record])
        self.assertIsNone(manager.refresh("wrong", "/tmp/root", 60))
        refreshed = manager.refresh(record.token, "/tmp/root", 120)
        self.assertIsNotNone(refreshed)
        self.assertFalse(manager.release(record.token, "/tmp/other"))
        self.assertFalse(manager.release("missing", "/tmp/root"))
        self.assertTrue(manager.release(record.token, "/tmp/root"))

    def test_timeout_parser_is_bounded_and_tolerant(self):
        self.assertEqual(_webdav._lock_timeout("Infinite"), 3600)
        self.assertEqual(_webdav._lock_timeout("nonsense"), 3600)
        self.assertEqual(_webdav._lock_timeout("Second-0"), 1)
        self.assertEqual(_webdav._lock_timeout("Second-999999"), 24 * 60 * 60)


class DavReadOnlyTest(_DavCase):
    dav_write = False

    def test_writes_blocked_but_reads_work(self):
        self.assertEqual(self._req("PUT", "/x.txt", body=b"x")[0], 403)  # read-only
        self.assertEqual(self._req("DELETE", "/hello.txt")[0], 403)
        self.assertEqual(self._req("PROPFIND", "/", headers={"Depth": "0"})[0], 207)  # reads ok
        _status, headers, _ = self._req("OPTIONS", "/")
        self.assertEqual(headers["DAV"], "1")
        self.assertEqual(self._req("LOCK", "/hello.txt", body=b"")[0], 405)


class DavDisabledTest(_DavCase):
    dav = False
    dav_write = False

    def test_dav_methods_unsupported_when_off(self):
        self.assertEqual(self._req("PROPFIND", "/")[0], 501)
        self.assertEqual(self._req("MKCOL", "/d")[0], 501)


class DavConfigTest(unittest.TestCase):
    def test_dav_write_requires_dav(self):
        with self.assertRaises(ValueError):
            Config.create(".", dav_write=True)


if __name__ == "__main__":
    unittest.main()
