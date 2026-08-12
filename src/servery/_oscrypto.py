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
import sys
import threading
from ctypes import c_char_p, c_int, c_void_p
from pathlib import Path

# macOS ships an OpenSSL *shim* at /usr/lib/libcrypto.dylib. Loading it through
# ctypes makes the system print "is loading libcrypto in an unsafe way" and then
# abort(3) the process — SIGABRT, which no ``except`` can catch, so probing it
# and falling back is not an option: the check itself has to refuse the path.
# A real OpenSSL installed alongside (Homebrew, MacPorts) loads fine.
_DARWIN_SYSTEM_PREFIX = "/usr/lib/"
_DARWIN_CANDIDATES = (
    "/opt/homebrew/opt/openssl@3/lib/libcrypto.dylib",  # Homebrew, Apple Silicon
    "/usr/local/opt/openssl@3/lib/libcrypto.dylib",  # Homebrew, Intel
    "/opt/local/lib/libcrypto.dylib",  # MacPorts
)

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
_lock = threading.Lock()


def is_darwin_system_libcrypto(path: str | None) -> bool:
    """True when ``path`` is macOS's system libcrypto, which aborts on load."""
    return bool(path) and path.startswith(_DARWIN_SYSTEM_PREFIX)


def _resolve_library() -> str:
    """The libcrypto to load, or raise if only an unloadable one is present.

    On macOS the system shim must never be handed to ``CDLL`` (see above), so a
    real OpenSSL is looked for first and the shim is rejected outright rather
    than tried. Everywhere else this is the usual ``find_library`` lookup.
    """
    if sys.platform == "darwin":
        for candidate in _DARWIN_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        found = ctypes.util.find_library("crypto")
        if is_darwin_system_libcrypto(found) or not found:
            raise CryptoUnavailableError(
                "macOS's system libcrypto aborts the process when loaded via "
                "ctypes; install a real OpenSSL (e.g. `brew install openssl@3`)"
            )
        return found
    return ctypes.util.find_library("crypto") or "libcrypto.so"


def _libcrypto() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    with _lock:  # configure once even under free-threading
        if _lib is not None:  # pragma: no cover - lost the init race
            return _lib
        name = _resolve_library()
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


def _check(result: int, what: str) -> None:
    if result != 1:
        raise CryptoUnavailableError(f"OpenSSL {what} failed")


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
        _check(lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_256_gcm(), None, None, None), "EncryptInit")
        _check(lib.EVP_CIPHER_CTX_ctrl(ctx, _GCM_SET_IVLEN, len(nonce), None), "set GCM IV len")
        _check(lib.EVP_EncryptInit_ex(ctx, None, None, key, nonce), "EncryptInit key/iv")
        outlen = c_int()
        if aad:
            _check(
                lib.EVP_EncryptUpdate(ctx, None, ctypes.byref(outlen), aad, len(aad)),
                "EncryptUpdate aad",
            )
        buffer = ctypes.create_string_buffer(len(plaintext) + 16)
        _check(
            lib.EVP_EncryptUpdate(ctx, buffer, ctypes.byref(outlen), plaintext, len(plaintext)),
            "EncryptUpdate",
        )
        ciphertext = buffer.raw[: outlen.value]
        final = ctypes.create_string_buffer(16)
        finlen = c_int()
        _check(lib.EVP_EncryptFinal_ex(ctx, final, ctypes.byref(finlen)), "EncryptFinal")
        ciphertext += final.raw[: finlen.value]
        tag = ctypes.create_string_buffer(_TAG_LEN)
        _check(lib.EVP_CIPHER_CTX_ctrl(ctx, _GCM_GET_TAG, _TAG_LEN, tag), "get GCM tag")
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
        _check(lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_256_gcm(), None, None, None), "DecryptInit")
        _check(lib.EVP_CIPHER_CTX_ctrl(ctx, _GCM_SET_IVLEN, len(nonce), None), "set GCM IV len")
        _check(lib.EVP_DecryptInit_ex(ctx, None, None, key, nonce), "DecryptInit key/iv")
        outlen = c_int()
        if aad:
            _check(
                lib.EVP_DecryptUpdate(ctx, None, ctypes.byref(outlen), aad, len(aad)),
                "DecryptUpdate aad",
            )
        buffer = ctypes.create_string_buffer(len(ciphertext) + 16)
        _check(
            lib.EVP_DecryptUpdate(ctx, buffer, ctypes.byref(outlen), ciphertext, len(ciphertext)),
            "DecryptUpdate",
        )
        plaintext = buffer.raw[: outlen.value]
        # SET_TAG must succeed before Final, or Final could pass without real verification.
        _check(lib.EVP_CIPHER_CTX_ctrl(ctx, _GCM_SET_TAG, _TAG_LEN, c_char_p(tag)), "set GCM tag")
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
