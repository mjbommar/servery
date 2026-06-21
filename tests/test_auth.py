"""Unit tests for HTTP Basic auth credential parsing and verification."""

import base64
import hashlib
import unittest

from servery import auth


def _basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


class ParseTest(unittest.TestCase):
    def test_plain(self):
        cred = auth.parse("alice:secret")
        assert cred is not None
        self.assertEqual(cred.username, "alice")
        self.assertEqual(cred.algorithm, "plain")
        self.assertTrue(cred.verify("alice", "secret"))
        self.assertFalse(cred.verify("alice", "wrong"))
        self.assertFalse(cred.verify("bob", "secret"))

    def test_sha256(self):
        digest = hashlib.sha256(b"secret").hexdigest()
        cred = auth.parse(f"alice:sha256:{digest}")
        assert cred is not None
        self.assertEqual(cred.algorithm, "sha256")
        self.assertTrue(cred.verify("alice", "secret"))
        self.assertFalse(cred.verify("alice", "nope"))

    def test_password_may_contain_colons(self):
        cred = auth.parse("alice:a:b:c")
        assert cred is not None
        self.assertEqual(cred.algorithm, "plain")
        self.assertTrue(cred.verify("alice", "a:b:c"))

    def test_none(self):
        self.assertIsNone(auth.parse(None))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            auth.parse("nouserorpass")
        with self.assertRaises(ValueError):
            auth.parse("alice:sha256:")


class CheckHeaderTest(unittest.TestCase):
    def setUp(self):
        cred = auth.parse("alice:secret")
        assert cred is not None
        self.cred = cred

    def test_valid(self):
        self.assertTrue(self.cred.check_header(_basic_header("alice", "secret")))

    def test_wrong_password(self):
        self.assertFalse(self.cred.check_header(_basic_header("alice", "x")))

    def test_non_basic_scheme(self):
        self.assertFalse(self.cred.check_header("Bearer abc.def"))

    def test_malformed_base64(self):
        self.assertFalse(self.cred.check_header("Basic not_base64!!"))

    def test_missing_colon(self):
        token = base64.b64encode(b"alicesecret").decode("ascii")
        self.assertFalse(self.cred.check_header(f"Basic {token}"))


if __name__ == "__main__":
    unittest.main()
