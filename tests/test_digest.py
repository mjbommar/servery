"""RFC 9530 integrity digest tests (Want-Repr-Digest -> Repr-Digest)."""

from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from servery import _digest
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


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
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


if __name__ == "__main__":
    unittest.main()
