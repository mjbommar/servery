"""RFC 9530 integrity digest tests (Want-Repr-Digest -> Repr-Digest)."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from servery import _digest, _static
from servery.config import Config
from tests._harness import raw_exchange, serving


class ChooseAlgorithmTest(unittest.TestCase):
    def test_none_when_not_asked(self):
        self.assertIsNone(_digest.choose_algorithm(None))

    def test_bare_key_is_wanted(self):
        self.assertEqual(_digest.choose_algorithm("sha-256"), "sha-256")

    def test_highest_preference_wins(self):
        self.assertEqual(_digest.choose_algorithm("sha-256=3, sha-512=10"), "sha-512")
        self.assertEqual(_digest.choose_algorithm("sha-256=10, sha-512=3"), "sha-256")

    def test_zero_preference_excluded(self):
        self.assertEqual(_digest.choose_algorithm("sha-256=0, sha-512=5"), "sha-512")
        self.assertIsNone(_digest.choose_algorithm("sha-256=0"))

    def test_unsupported_only_is_none(self):
        self.assertIsNone(_digest.choose_algorithm("sha, md5=10"))

    def test_boolean_forms(self):
        self.assertEqual(_digest.choose_algorithm("sha-256=?1"), "sha-256")
        self.assertIsNone(_digest.choose_algorithm("sha-256=?0"))

    def test_tolerates_garbage_value_and_empty_members(self):
        # An unparseable preference is treated as "wanted"; blank/keyless members skip.
        self.assertEqual(_digest.choose_algorithm("sha-256=notanumber"), "sha-256")
        self.assertEqual(_digest.choose_algorithm(", =5, sha-512=2"), "sha-512")


class FieldValueTest(unittest.TestCase):
    def test_sha256_field_value(self):
        data = b"servery digest"
        expected = base64.b64encode(hashlib.sha256(data).digest()).decode()
        self.assertEqual(_digest.field_value("sha-256", data), f"sha-256=:{expected}:")

    def test_sha512_field_value(self):
        data = b"x" * 1000
        expected = base64.b64encode(hashlib.sha512(data).digest()).decode()
        self.assertEqual(_digest.field_value("sha-512", data), f"sha-512=:{expected}:")

    def test_file_matches_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.bin"
            data = b"abc" * 5000
            path.write_bytes(data)
            self.assertEqual(
                _digest.field_value_for_file(str(path), "sha-256"),
                _digest.field_value("sha-256", data),
            )

    def test_missing_file_is_none(self):
        self.assertIsNone(_digest.field_value_for_file("/no/such/file", "sha-256"))

    def test_handle_hash_restores_position_and_rejects_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "identity.bin")
            path.write_bytes(b"abcdef")
            with path.open("r+b") as handle:
                handle.seek(2)
                self.assertEqual(
                    _digest.field_value_for_handle(handle, "sha-256", 6),
                    _digest.field_value("sha-256", b"abcdef"),
                )
                self.assertEqual(handle.tell(), 2)
                os.truncate(path, 3)
                with self.assertRaises(OSError):
                    _digest.field_value_for_handle(handle, "sha-256", 6)
                self.assertEqual(handle.tell(), 2)


class DigestCacheTest(unittest.TestCase):
    def _key(self, path: Path, algorithm: str = "sha-256") -> _digest.CacheKey:
        return _digest.cache_key(str(path), path.stat(), algorithm)

    def test_zero_retention_shares_only_concurrent_same_key_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "hot.bin")
            path.write_bytes(b"hot")
            cache = _digest.DigestCache(0)
            key = self._key(path)
            entered = threading.Event()
            release = threading.Event()
            calls = 0
            results: list[str] = []

            def factory() -> str:
                nonlocal calls
                calls += 1
                entered.set()
                release.wait(1)
                return "sha-256=:value:"

            first = threading.Thread(
                target=lambda: results.append(cache.get_or_compute(key, factory))
            )
            second = threading.Thread(
                target=lambda: results.append(cache.get_or_compute(key, factory))
            )
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            time.sleep(0.01)
            release.set()
            first.join(2)
            second.join(2)

            self.assertEqual(calls, 1)
            self.assertEqual(results, ["sha-256=:value:"] * 2)
            self.assertEqual(cache.current_entries, 0)
            self.assertIsNone(cache.get(key))
            self.assertEqual(cache.get_or_compute(key, lambda: "later"), "later")

    def test_entry_bound_lru_and_algorithm_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "hot.bin")
            path.write_bytes(b"hot")
            cache = _digest.DigestCache(1)
            sha256 = self._key(path)
            sha512 = self._key(path, "sha-512")
            cache.put(sha256, "256")
            cache.put(sha512, "512")
            self.assertIsNone(cache.get(sha256))
            self.assertEqual(cache.get(sha512), "512")
            self.assertEqual(cache.current_entries, 1)

    def test_distinct_keys_compute_concurrently_and_failures_are_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp, "first")
            second_path = Path(tmp, "second")
            first_path.write_bytes(b"1")
            second_path.write_bytes(b"2")
            cache = _digest.DigestCache()
            rendezvous = threading.Barrier(2, timeout=1)
            results: list[str] = []

            def worker(path: Path) -> None:
                results.append(
                    cache.get_or_compute(
                        self._key(path),
                        lambda: (rendezvous.wait(), path.name)[1],
                    )
                )

            threads = [
                threading.Thread(target=worker, args=(path,)) for path in (first_path, second_path)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertCountEqual(results, ["first", "second"])
            self.assertEqual(cache._flights, {})

            key = self._key(first_path)
            with self.assertRaisesRegex(OSError, "hash failed"):
                cache.get_or_compute(key, lambda: (_ for _ in ()).throw(OSError("hash failed")))
            self.assertEqual(cache._flights, {})


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        # Incompressible bytes so it is served identity even when zstd/gzip is offered.
        self.data = bytes(range(256)) * 64  # 16 KiB, > the compression floor
        (root / "blob.bin").write_bytes(self.data)
        (root / "page.html").write_bytes(b"<h1>hi</h1>\n" + b"x" * 4000)  # compressible
        self.cfg = Config.create(str(root), host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _get(self, path, *, want=None, extra=b""):
        head = f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n".encode()
        if want is not None:
            head += f"Want-Repr-Digest: {want}\r\n".encode()
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(host, port, head + extra + b"\r\n")
        head_b, _, body = resp.partition(b"\r\n\r\n")
        return head_b, body

    def _digest_line(self, head):
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"repr-digest:"):
                return line.split(b":", 1)[1].strip().decode()
        return None


class ReprDigestServerTest(_ServerCase):
    def test_absent_when_not_requested(self):
        head, _ = self._get("/blob.bin")
        self.assertIsNone(self._digest_line(head))

    def test_emitted_and_correct(self):
        head, body = self._get("/blob.bin", want="sha-256")
        expected = _digest.field_value("sha-256", self.data)
        self.assertEqual(self._digest_line(head), expected)
        self.assertEqual(body, self.data)

    def test_sha512_selected(self):
        head, _ = self._get("/blob.bin", want="sha-512=10, sha-256=1")
        self.assertEqual(self._digest_line(head), _digest.field_value("sha-512", self.data))

    def test_range_gets_full_representation_digest(self):
        # Repr-Digest is over the FULL file, even for a 206 — so a parallel/ranged
        # download can verify the reassembled whole.
        head, body = self._get("/blob.bin", want="sha-256", extra=b"Range: bytes=0-9\r\n")
        self.assertIn(b"206", head.split(b"\r\n", 1)[0])
        self.assertEqual(self._digest_line(head), _digest.field_value("sha-256", self.data))
        self.assertEqual(body, self.data[:10])

    def test_unsupported_request_no_header(self):
        head, _ = self._get("/blob.bin", want="md5=10")
        self.assertIsNone(self._digest_line(head))

    def test_coded_response_has_no_repr_digest(self):
        # A compressible file fetched with compression offered is content-coded; the
        # representation is no longer the identity file, so we omit Repr-Digest.
        head, _ = self._get("/page.html", want="sha-256", extra=b"Accept-Encoding: gzip\r\n")
        self.assertIn(b"content-encoding", head.lower())
        self.assertIsNone(self._digest_line(head))

    @unittest.skipIf(os.name == "nt", "Windows cannot replace an open file")
    def test_digest_and_body_survive_atomic_path_replacement_as_one_identity(self):
        original_open = _static.open_file
        replaced = False

        def open_then_replace(*args, **kwargs):
            nonlocal replaced
            opened = original_open(*args, **kwargs)
            if not replaced and Path(args[0]).name == "blob.bin":
                replaced = True
                replacement = self.root / "replacement.tmp"
                replacement.write_bytes(b"replacement")
                replacement.replace(self.root / "blob.bin")
            return opened

        with mock.patch.object(_static, "open_file", side_effect=open_then_replace):
            head, body = self._get("/blob.bin", want="sha-256")
        self.assertEqual(body, self.data)
        self.assertEqual(self._digest_line(head), _digest.field_value("sha-256", self.data))

    def test_truncation_during_digest_fails_before_file_headers(self):
        original_open = _static.open_file
        truncated = False

        def open_then_truncate(*args, **kwargs):
            nonlocal truncated
            opened = original_open(*args, **kwargs)
            if not truncated and Path(args[0]).name == "blob.bin":
                truncated = True
                os.truncate(self.root / "blob.bin", 2)
            return opened

        with mock.patch.object(_static, "open_file", side_effect=open_then_truncate):
            head, body = self._get("/blob.bin", want="sha-256")
        self.assertIn(b"500", head.split(b"\r\n", 1)[0])
        self.assertIsNone(self._digest_line(head))
        self.assertNotEqual(body, self.data[:2])


if __name__ == "__main__":
    unittest.main()
