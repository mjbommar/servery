"""Zero-PyPI-dependency AEAD via ``ctypes`` → the OS OpenSSL ``libcrypto``.

The Python standard library ships no symmetric ciphers, yet every CPython is
linked against OpenSSL. So we can reach AES-256-GCM through ``ctypes`` (itself
stdlib) with no third-party package — only a runtime dependency on a system
library that is already loaded in-process.

This is the crypto foundation a *native* (zero-dependency) QUIC / HTTP-3 backend
would build on: QUIC protects every packet with exactly this AEAD. It proves the
``docs/TRANSPORTS.md`` thesis that the "no stdlib crypto" wall is tunnelable via
ctypes-to-OS-crypto without adding a PyPI dependency. A full QUIC transport on
top of this remains a large, separate effort; HTTP/3 today is the optional
``servery[http3]`` (aioquic) backend.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import c_char_p, c_int, c_void_p

_GCM_SET_IVLEN = 0x9
_GCM_GET_TAG = 0x10
_GCM_SET_TAG = 0x11
_TAG_LEN = 16
_KEY_LEN = 32
_NONCE_LEN = 12


class CryptoUnavailableError(RuntimeError):
    """OpenSSL's libcrypto could not be loaded via ctypes."""


class AuthenticationError(ValueError):
    """AEAD tag verification failed (ciphertext was tampered with or corrupt)."""


_lib: ctypes.CDLL | None = None


def _libcrypto() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    name = ctypes.util.find_library("crypto") or "libcrypto.so"
    try:
        lib = ctypes.CDLL(name)
    except OSError as exc:  # pragma: no cover - platform without OpenSSL
        raise CryptoUnavailableError(f"could not load libcrypto ({name})") from exc
    lib.EVP_CIPHER_CTX_new.restype = c_void_p
    lib.EVP_CIPHER_CTX_free.argtypes = [c_void_p]
    lib.EVP_aes_256_gcm.restype = c_void_p
    for name_ in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
        getattr(lib, name_).argtypes = [c_void_p, c_void_p, c_void_p, c_char_p, c_char_p]
    for name_ in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
        getattr(lib, name_).argtypes = [c_void_p, c_char_p, c_void_p, c_char_p, c_int]
    for name_ in ("EVP_EncryptFinal_ex", "EVP_DecryptFinal_ex"):
        getattr(lib, name_).argtypes = [c_void_p, c_char_p, c_void_p]
    lib.EVP_CIPHER_CTX_ctrl.argtypes = [c_void_p, c_int, c_int, c_void_p]
    _lib = lib
    return lib


def available() -> bool:
    """True if OS OpenSSL crypto can be loaded."""
    try:
        _libcrypto()
    except CryptoUnavailableError:  # pragma: no cover - platform without OpenSSL
        return False
    return True


def aes_256_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Encrypt with AES-256-GCM. Returns ``ciphertext || tag`` (tag is 16 bytes)."""
    _check_key_nonce(key, nonce)
    lib = _libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise CryptoUnavailableError("EVP_CIPHER_CTX_new failed")
    try:
        if not lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_256_gcm(), None, None, None):
            raise CryptoUnavailableError("EncryptInit failed")
        lib.EVP_CIPHER_CTX_ctrl(ctx, _GCM_SET_IVLEN, len(nonce), None)
        if not lib.EVP_EncryptInit_ex(ctx, None, None, key, nonce):
            raise CryptoUnavailableError("EncryptInit (key/iv) failed")
        outlen = c_int()
        if aad:
            lib.EVP_EncryptUpdate(ctx, None, ctypes.byref(outlen), aad, len(aad))
        buffer = ctypes.create_string_buffer(len(plaintext) + 16)
        if not lib.EVP_EncryptUpdate(ctx, buffer, ctypes.byref(outlen), plaintext, len(plaintext)):
            raise CryptoUnavailableError("EncryptUpdate failed")
        ciphertext = buffer.raw[: outlen.value]
        final = ctypes.create_string_buffer(16)
        finlen = c_int()
        lib.EVP_EncryptFinal_ex(ctx, final, ctypes.byref(finlen))
        ciphertext += final.raw[: finlen.value]
        tag = ctypes.create_string_buffer(_TAG_LEN)
        lib.EVP_CIPHER_CTX_ctrl(ctx, _GCM_GET_TAG, _TAG_LEN, tag)
        return ciphertext + tag.raw[:_TAG_LEN]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def aes_256_gcm_decrypt(
    key: bytes, nonce: bytes, ciphertext_and_tag: bytes, aad: bytes = b""
) -> bytes:
    """Decrypt+verify AES-256-GCM (input is ``ciphertext || tag``). Raises on bad tag."""
    _check_key_nonce(key, nonce)
    if len(ciphertext_and_tag) < _TAG_LEN:
        raise AuthenticationError("input shorter than the authentication tag")
    ciphertext = ciphertext_and_tag[:-_TAG_LEN]
    tag = ciphertext_and_tag[-_TAG_LEN:]
    lib = _libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise CryptoUnavailableError("EVP_CIPHER_CTX_new failed")
    try:
        lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_256_gcm(), None, None, None)
        lib.EVP_CIPHER_CTX_ctrl(ctx, _GCM_SET_IVLEN, len(nonce), None)
        lib.EVP_DecryptInit_ex(ctx, None, None, key, nonce)
        outlen = c_int()
        if aad:
            lib.EVP_DecryptUpdate(ctx, None, ctypes.byref(outlen), aad, len(aad))
        buffer = ctypes.create_string_buffer(len(ciphertext) + 16)
        lib.EVP_DecryptUpdate(ctx, buffer, ctypes.byref(outlen), ciphertext, len(ciphertext))
        plaintext = buffer.raw[: outlen.value]
        lib.EVP_CIPHER_CTX_ctrl(ctx, _GCM_SET_TAG, _TAG_LEN, c_char_p(tag))
        final = ctypes.create_string_buffer(16)
        finlen = c_int()
        if lib.EVP_DecryptFinal_ex(ctx, final, ctypes.byref(finlen)) != 1:
            raise AuthenticationError("AES-256-GCM tag verification failed")
        return plaintext + final.raw[: finlen.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _check_key_nonce(key: bytes, nonce: bytes) -> None:
    if len(key) != _KEY_LEN:
        raise ValueError("AES-256-GCM key must be 32 bytes")
    if len(nonce) != _NONCE_LEN:
        raise ValueError("AES-256-GCM nonce must be 12 bytes")
