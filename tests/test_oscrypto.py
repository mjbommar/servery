"""Tests for the ctypes->OpenSSL AES-256-GCM AEAD (zero-PyPI-dependency crypto).

Verified against the standard NIST AES-256-GCM known-answer vectors, proving the
ctypes binding actually computes correct, interoperable AEAD.
"""

import unittest
import unittest.mock

from servery import _oscrypto

_HAVE_CRYPTO = _oscrypto.available()


@unittest.skipUnless(_HAVE_CRYPTO, "OS OpenSSL libcrypto not available")
class AesGcmTest(unittest.TestCase):
    def test_nist_empty_plaintext_vector(self):
        # NIST: key=0*32, iv=0*12, no plaintext/aad -> tag 530f8afbc74536b9a963b4f1c4cb738b
        out = _oscrypto.aes_256_gcm_encrypt(b"\x00" * 32, b"\x00" * 12, b"")
        self.assertEqual(out.hex(), "530f8afbc74536b9a963b4f1c4cb738b")

    def test_nist_block_plaintext_vector(self):
        # NIST: key=0*32, iv=0*12, pt=0*16 -> ct cea7403d4d606b6e074ec5d3baf39d18,
        # tag d0d1c8a799996bf0265b98b5d48ab919
        out = _oscrypto.aes_256_gcm_encrypt(b"\x00" * 32, b"\x00" * 12, b"\x00" * 16)
        self.assertEqual(
            out.hex(),
            "cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919",
        )

    def test_round_trip_with_aad(self):
        key = bytes(range(32))
        nonce = bytes(range(12))
        message = b"servery proves zero-dep AEAD via ctypes"
        aad = b"quic-header"
        sealed = _oscrypto.aes_256_gcm_encrypt(key, nonce, message, aad)
        opened = _oscrypto.aes_256_gcm_decrypt(key, nonce, sealed, aad)
        self.assertEqual(opened, message)

    def test_tampered_ciphertext_is_rejected(self):
        key = bytes(range(32))
        nonce = bytes(range(12))
        sealed = bytearray(_oscrypto.aes_256_gcm_encrypt(key, nonce, b"secret"))
        sealed[0] ^= 0x01
        with self.assertRaises(_oscrypto.AuthenticationError):
            _oscrypto.aes_256_gcm_decrypt(key, nonce, bytes(sealed))

    def test_wrong_aad_is_rejected(self):
        key = bytes(range(32))
        nonce = bytes(range(12))
        sealed = _oscrypto.aes_256_gcm_encrypt(key, nonce, b"secret", b"aad-1")
        with self.assertRaises(_oscrypto.AuthenticationError):
            _oscrypto.aes_256_gcm_decrypt(key, nonce, sealed, b"aad-2")

    def test_bad_key_length(self):
        with self.assertRaises(ValueError):
            _oscrypto.aes_256_gcm_encrypt(b"short", b"\x00" * 12, b"x")


if __name__ == "__main__":
    unittest.main()


class DarwinLibcryptoTest(unittest.TestCase):
    """macOS's system libcrypto abort()s on ctypes load, so it must be refused.

    ``abort(3)`` raises SIGABRT, which no ``except`` can catch — probing the
    path and recovering is impossible, so ``_resolve_library`` has to reject it
    without ever calling ``CDLL``. Regression test for the release gate that
    died at exit 134 during test discovery.
    """

    def test_system_shim_is_recognized(self):
        self.assertTrue(_oscrypto.is_darwin_system_libcrypto("/usr/lib/libcrypto.dylib"))
        self.assertTrue(_oscrypto.is_darwin_system_libcrypto("/usr/lib/libcrypto.35.dylib"))
        self.assertFalse(_oscrypto.is_darwin_system_libcrypto(None))
        self.assertFalse(
            _oscrypto.is_darwin_system_libcrypto("/opt/homebrew/opt/openssl@3/lib/libcrypto.dylib")
        )

    def test_darwin_prefers_a_real_openssl(self):
        real = "/opt/homebrew/opt/openssl@3/lib/libcrypto.dylib"
        with (
            unittest.mock.patch("sys.platform", "darwin"),
            unittest.mock.patch("pathlib.Path.exists", lambda self: str(self) == real),
        ):
            self.assertEqual(_oscrypto._resolve_library(), real)

    def test_darwin_refuses_the_system_shim(self):
        with (
            unittest.mock.patch("sys.platform", "darwin"),
            unittest.mock.patch("pathlib.Path.exists", lambda _self: False),
            unittest.mock.patch("ctypes.util.find_library", lambda _n: "/usr/lib/libcrypto.dylib"),
            self.assertRaises(_oscrypto.CryptoUnavailableError),
        ):
            _oscrypto._resolve_library()

    def test_darwin_without_any_libcrypto(self):
        with (
            unittest.mock.patch("sys.platform", "darwin"),
            unittest.mock.patch("pathlib.Path.exists", lambda _self: False),
            unittest.mock.patch("ctypes.util.find_library", lambda _n: None),
            self.assertRaises(_oscrypto.CryptoUnavailableError),
        ):
            _oscrypto._resolve_library()

    def test_other_platforms_use_find_library(self):
        with (
            unittest.mock.patch("sys.platform", "linux"),
            unittest.mock.patch("ctypes.util.find_library", lambda _n: "libcrypto.so.3"),
        ):
            self.assertEqual(_oscrypto._resolve_library(), "libcrypto.so.3")
