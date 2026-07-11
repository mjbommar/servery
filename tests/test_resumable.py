"""Resumable Content-Range PUT upload tests."""

from __future__ import annotations

import http.client
import io
import tempfile
import unittest
from pathlib import Path

from servery import _resumable
from servery.config import Config
from tests._harness import raw_exchange, serving


class ParseContentRangeTest(unittest.TestCase):
    def test_data_form(self):
        cr = _resumable.parse_content_range("bytes 0-9/100")
        self.assertEqual((cr.start, cr.end, cr.total), (0, 9, 100))
        self.assertFalse(cr.is_query)
        self.assertEqual(cr.length, 10)

    def test_unknown_total(self):
        cr = _resumable.parse_content_range("bytes 10-19/*")
        self.assertEqual((cr.start, cr.end, cr.total), (10, 19, None))

    def test_query_form(self):
        cr = _resumable.parse_content_range("bytes */500")
        self.assertTrue(cr.is_query)
        self.assertEqual(cr.total, 500)

    def test_length_undefined_for_query(self):
        cr = _resumable.parse_content_range("bytes */500")
        with self.assertRaises(ValueError):
            _ = cr.length

    def test_range_without_dash(self):
        with self.assertRaises(_resumable.ResumableError):
            _resumable.parse_content_range("bytes 5/100")

    def test_errors(self):
        for bad in (
            "0-9/100",  # no unit
            "bytes 0-9",  # no /total
            "bytes */*",  # meaningless
            "bytes 9-0/100",  # start after end
            "bytes 0-100/100",  # end >= total
            "bytes a-9/100",  # non-integer
            "bytes -5/100",  # malformed range
        ):
            with self.assertRaises(_resumable.ResumableError, msg=bad):
                _resumable.parse_content_range(bad)

    def test_short_whole_write_never_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "short.bin")
            with self.assertRaises(OSError):
                _resumable.write_whole(target, io.BytesIO(b"short"), 10)
            self.assertFalse(Path(target).exists())
            self.assertFalse(Path(_resumable.part_path(target)).exists())

    def test_partial_budget_disabled_and_external_cleanup_reclaims_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            first = str(Path(directory) / f".first{_resumable.PART_SUFFIX}")
            second = str(Path(directory) / f".second{_resumable.PART_SUFFIX}")
            Path(first).write_bytes(b"x")
            budget = _resumable.PartialUploadBudget(directory, 1)
            self.assertFalse(budget.claim(second))
            Path(first).unlink()
            self.assertTrue(budget.claim(second))
            budget.release(second)

            disabled = _resumable.PartialUploadBudget(directory, 0)
            self.assertTrue(disabled.claim(first))
            disabled.release(first)

    def test_stale_sidecar_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            part = Path(directory) / f".old{_resumable.PART_SUFFIX}"
            self.assertFalse(_resumable.discard_stale(str(part), 0))
            self.assertFalse(_resumable.discard_stale(str(part), 10, now=100))
            part.write_bytes(b"x")
            self.assertFalse(_resumable.discard_stale(str(part), 10, now=part.stat().st_mtime + 5))
            self.assertTrue(_resumable.discard_stale(str(part), 10, now=part.stat().st_mtime + 11))


def _put(host: str, port: int, path: str, body: bytes, headers: dict | None = None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("PUT", path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


class _ServerCase(unittest.TestCase):
    upload = True
    allow_overwrite = False
    max_upload_size = 100 * 1024 * 1024

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = Config.create(
            str(self.root),
            host="127.0.0.1",
            port=0,
            quiet=True,
            upload=self.upload,
            allow_overwrite=self.allow_overwrite,
            max_upload_size=self.max_upload_size,
        )

    def tearDown(self):
        self._tmp.cleanup()


class WholePutTest(_ServerCase):
    def test_create(self):
        with serving(self.cfg) as (host, port):
            status, headers, _ = _put(host, port, "/new.txt", b"hello world")
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("location"), "/new.txt")
        self.assertEqual((self.root / "new.txt").read_bytes(), b"hello world")

    def test_whole_put_supersedes_old_partial_sidecar(self):
        part = self.root / f".new.txt{_resumable.PART_SUFFIX}"
        part.write_bytes(b"stale partial")
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/new.txt", b"complete")
        self.assertEqual(status, 201)
        self.assertEqual((self.root / "new.txt").read_bytes(), b"complete")
        self.assertFalse(part.exists())

    def test_overwrite_refused_by_default(self):
        (self.root / "x.txt").write_bytes(b"old")
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/x.txt", b"new")
        self.assertEqual(status, 409)
        self.assertEqual((self.root / "x.txt").read_bytes(), b"old")

    def test_small_rejected_body_is_drained_for_keepalive(self):
        (self.root / "x.txt").write_bytes(b"old")
        cfg = Config.create(
            self.root,
            host="127.0.0.1",
            port=0,
            quiet=True,
            upload=True,
            keepalive_drain_limit=4,
        )
        with serving(cfg) as (host, port):
            response = raw_exchange(
                host,
                port,
                b"PUT /x.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\nDATA"
                b"GET /x.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
        self.assertEqual(response.count(b"HTTP/1.1 409"), 1)
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 1)

    def test_large_rejected_body_closes_at_configured_drain_limit(self):
        (self.root / "x.txt").write_bytes(b"old")
        cfg = Config.create(
            self.root,
            host="127.0.0.1",
            port=0,
            quiet=True,
            upload=True,
            keepalive_drain_limit=3,
        )
        with serving(cfg) as (host, port):
            response = raw_exchange(
                host,
                port,
                b"PUT /x.txt HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\nDATA"
                b"GET /x.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
        self.assertEqual(response.count(b"HTTP/1.1 409"), 1)
        self.assertNotIn(b"HTTP/1.1 200 OK", response)

    def test_directory_target_rejected(self):
        (self.root / "sub").mkdir()
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/sub", b"data")
        self.assertEqual(status, 409)

    def test_missing_parent_dir(self):
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/nope/deep.txt", b"data")
        self.assertEqual(status, 404)


class OverwriteAllowedTest(_ServerCase):
    allow_overwrite = True

    def test_overwrite(self):
        (self.root / "x.txt").write_bytes(b"old")
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/x.txt", b"brand new")
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "x.txt").read_bytes(), b"brand new")

    def test_resumable_overwrite_returns_200(self):
        (self.root / "y.bin").write_bytes(b"older content")
        with serving(self.cfg) as (host, port):
            _put(host, port, "/y.bin", b"AAAAA", {"Content-Range": "bytes 0-4/10"})
            status, _, _ = _put(host, port, "/y.bin", b"BBBBB", {"Content-Range": "bytes 5-9/10"})
        self.assertEqual(status, 200)  # an existing file was replaced (not created)
        self.assertEqual((self.root / "y.bin").read_bytes(), b"AAAAABBBBB")


class ResumableFlowTest(_ServerCase):
    def test_two_chunks_complete(self):
        data = b"A" * 10 + b"B" * 10  # total 20
        with serving(self.cfg) as (host, port):
            s1, h1, _ = _put(host, port, "/big.bin", data[:10], {"Content-Range": "bytes 0-9/20"})
            self.assertEqual(s1, 308)
            self.assertEqual(h1.get("range"), "bytes=0-9")
            # Mid-upload, the target is not yet visible; a hidden sidecar holds it.
            self.assertFalse((self.root / "big.bin").exists())

            sq, hq, _ = _put(host, port, "/big.bin", b"", {"Content-Range": "bytes */20"})
            self.assertEqual(sq, 308)
            self.assertEqual(hq.get("range"), "bytes=0-9")

            s2, _, _ = _put(host, port, "/big.bin", data[10:], {"Content-Range": "bytes 10-19/20"})
            self.assertEqual(s2, 201)
        self.assertEqual((self.root / "big.bin").read_bytes(), data)
        # The sidecar is gone after completion.
        self.assertFalse(_resumable_sidecar_exists(self.root, "big.bin"))

    def test_out_of_order_chunk_conflicts(self):
        with serving(self.cfg) as (host, port):
            _put(host, port, "/f.bin", b"0123456789", {"Content-Range": "bytes 0-9/30"})
            # Skip ahead — server has 10 bytes, client claims to start at 20.
            status, headers, _ = _put(
                host, port, "/f.bin", b"xxxxxxxxxx", {"Content-Range": "bytes 20-29/30"}
            )
        self.assertEqual(status, 409)
        self.assertEqual(headers.get("range"), "bytes=0-9")

    def test_length_must_match_range(self):
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/f.bin", b"short", {"Content-Range": "bytes 0-99/100"})
        self.assertEqual(status, 400)

    def test_query_before_any_chunk(self):
        with serving(self.cfg) as (host, port):
            status, headers, _ = _put(host, port, "/f.bin", b"", {"Content-Range": "bytes */50"})
        self.assertEqual(status, 308)
        self.assertNotIn("range", headers)  # nothing stored yet

    def test_unknown_total_then_final_chunk(self):
        with serving(self.cfg) as (host, port):
            s1, _, _ = _put(host, port, "/u.bin", b"AAAA", {"Content-Range": "bytes 0-3/*"})
            self.assertEqual(s1, 308)  # total unknown -> never completes on this chunk
            s2, _, _ = _put(host, port, "/u.bin", b"BBBB", {"Content-Range": "bytes 4-7/8"})
            self.assertEqual(s2, 201)
        self.assertEqual((self.root / "u.bin").read_bytes(), b"AAAABBBB")

    def test_bad_content_range_is_400(self):
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/f.bin", b"x", {"Content-Range": "rubbish"})
        self.assertEqual(status, 400)


class SizeLimitTest(_ServerCase):
    max_upload_size = 10

    def test_whole_too_big(self):
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/big.bin", b"x" * 20)
        self.assertEqual(status, 413)

    def test_ranged_total_too_big(self):
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/big.bin", b"x", {"Content-Range": "bytes 0-0/99"})
        self.assertEqual(status, 413)


class PartialUploadLimitTest(_ServerCase):
    def setUp(self):
        super().setUp()
        self.cfg = Config.create(
            str(self.root),
            host="127.0.0.1",
            port=0,
            quiet=True,
            upload=True,
            max_partial_uploads=1,
            # This test isolates partial-upload accounting. A zero-timeout target
            # lock can race the preceding handler's post-response cleanup and
            # turn the immediate retry into an unrelated transient 409.
            write_lock_timeout=0.1,
        )

    def test_completion_releases_capacity(self):
        with serving(self.cfg) as (host, port):
            first, _, _ = _put(host, port, "/one.bin", b"a", {"Content-Range": "bytes 0-0/2"})
            blocked, _, _ = _put(host, port, "/two.bin", b"a", {"Content-Range": "bytes 0-0/2"})
            completed, _, _ = _put(host, port, "/one.bin", b"b", {"Content-Range": "bytes 1-1/2"})
            admitted, _, _ = _put(host, port, "/two.bin", b"a", {"Content-Range": "bytes 0-0/2"})
        self.assertEqual((first, blocked, completed, admitted), (308, 507, 201, 308))

    def test_existing_sidecar_counts_against_limit(self):
        (self.root / f".existing.bin{_resumable.PART_SUFFIX}").write_bytes(b"a")
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/new.bin", b"a", {"Content-Range": "bytes 0-0/2"})
        self.assertEqual(status, 507)


class UploadDisabledTest(_ServerCase):
    upload = False

    def test_put_not_implemented(self):
        with serving(self.cfg) as (host, port):
            status, _, _ = _put(host, port, "/x.txt", b"data")
        self.assertEqual(status, 501)


def _resumable_sidecar_exists(root: Path, name: str) -> bool:
    return (root / f".{name}{_resumable.PART_SUFFIX}").exists()


if __name__ == "__main__":
    unittest.main()
