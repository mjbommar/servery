"""WebDAV (RFC 4918) tests: the mount-critical methods + Destination containment."""

from __future__ import annotations

import http.client
import tempfile
import unittest
from pathlib import Path

from servery.config import Config
from tests._harness import serving


class _DavCase(unittest.TestCase):
    dav = True
    dav_write = True
    allow_overwrite = False

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

    def test_lock_returns_token(self):
        status, hdrs, body = self._req(
            "LOCK",
            "/hello.txt",
            body=b'<D:lockinfo xmlns:D="DAV:"><D:lockscope>'
            b"<D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype></D:lockinfo>",
        )
        self.assertEqual(status, 200)
        self.assertTrue(hdrs.get("Lock-Token", "").startswith("<opaquelocktoken:"))
        self.assertIn(b"activelock", body)
        self.assertEqual(self._req("UNLOCK", "/hello.txt")[0], 204)


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


class DavReadOnlyTest(_DavCase):
    dav_write = False

    def test_writes_blocked_but_reads_work(self):
        self.assertEqual(self._req("PUT", "/x.txt", body=b"x")[0], 403)  # read-only
        self.assertEqual(self._req("DELETE", "/hello.txt")[0], 403)
        self.assertEqual(self._req("PROPFIND", "/", headers={"Depth": "0"})[0], 207)  # reads ok
        self.assertEqual(self._req("LOCK", "/hello.txt", body=b"")[0], 200)  # stub lock still works


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
