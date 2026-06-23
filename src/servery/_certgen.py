"""Self-signed TLS certificate generation in pure Python (zero dependencies).

The standard library can *use* TLS (``ssl``) but offers no way to *mint* a
certificate — no X.509 builder, no asymmetric key generation. This module fills
that gap with the few primitives the stdlib does provide: arbitrary-precision
``pow`` (modular exponentiation + inverse), ``hashlib`` (SHA-256), and
``secrets`` (a CSPRNG). That is enough to generate an RSA key and a self-signed
certificate without any third-party package, on a plain Windows or Linux Python.

Scope and security posture — read this before reaching for it:

* This produces an **ad-hoc, self-signed** certificate for **opportunistic
  encryption** on a dev box or LAN. It is **not a trust anchor**: clients still
  see an "untrusted certificate" warning unless they explicitly trust it. It is
  emphatically not for the public internet — that is what ACME / Let's Encrypt
  (the optional ``servery[acme]`` extra) is for.
* The only hand-rolled cryptography here is **key generation and signing our own
  certificate once at startup**. The TLS handshake, key exchange, and record
  encryption are all performed by OpenSSL via the stdlib ``ssl`` module — none of
  that is reimplemented. The side-channel concerns that plague hand-rolled crypto
  (timing, padding oracles) do not apply to one-shot self-cert generation.
* Keys come from ``secrets`` (CSPRNG); RSA-2048 + SHA-256; primes are checked
  with 40 rounds of Miller-Rabin. Generation takes well under a second.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import secrets
import time
from collections.abc import Sequence

# --- RSA key generation ------------------------------------------------------

_SMALL_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47)


def _probably_prime(n: int, rounds: int = 40) -> bool:
    """Miller-Rabin primality test (probabilistic; 40 rounds is ample)."""
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _random_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _probably_prime(candidate):
            return candidate


def _generate_rsa(bits: int) -> dict[str, int]:
    """Return RSA parameters (with CRT values) for a fresh key of ``bits`` size."""
    e = 65537
    while True:
        p = _random_prime(bits // 2)
        q = _random_prime(bits // 2)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        d = pow(e, -1, phi)
        return {
            "n": n,
            "e": e,
            "d": d,
            "p": p,
            "q": q,
            "dp": d % (p - 1),
            "dq": d % (q - 1),
            "qinv": pow(q, -1, p),
        }


# --- minimal DER (ASN.1) encoder ---------------------------------------------


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(value)) + value


def _int(x: int) -> bytes:
    if x == 0:
        content = b"\x00"
    else:
        content = x.to_bytes((x.bit_length() + 7) // 8, "big")
        if content[0] & 0x80:  # keep it positive
            content = b"\x00" + content
    return _tlv(0x02, content)


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _set(*items: bytes) -> bytes:
    return _tlv(0x31, b"".join(items))


def _bitstring(data: bytes) -> bytes:
    return _tlv(0x03, b"\x00" + data)


def _octets(data: bytes) -> bytes:
    return _tlv(0x04, data)


def _null() -> bytes:
    return b"\x05\x00"


def _bool(value: bool) -> bytes:
    return _tlv(0x01, b"\xff" if value else b"\x00")


def _ctx(num: int, content: bytes) -> bytes:  # [num] explicit, constructed
    return _tlv(0xA0 + num, content)


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    body = [40 * parts[0] + parts[1]]
    for value in parts[2:]:
        chunk = [value & 0x7F]
        value >>= 7
        while value:
            chunk.append((value & 0x7F) | 0x80)
            value >>= 7
        body.extend(reversed(chunk))
    return _tlv(0x06, bytes(body))


def _printable(text: str) -> bytes:
    return _tlv(0x13, text.encode("ascii"))


def _utctime(epoch: int) -> bytes:
    return _tlv(0x17, time.strftime("%y%m%d%H%M%SZ", time.gmtime(epoch)).encode("ascii"))


_OID = {
    "rsa": "1.2.840.113549.1.1.1",
    "sha256_rsa": "1.2.840.113549.1.1.11",
    "sha256": "2.16.840.1.101.3.4.2.1",
    "common_name": "2.5.4.3",
    "san": "2.5.29.17",
    "ext_request": "1.2.840.113549.1.9.14",  # PKCS#9 extensionRequest (CSR attribute)
    "basic_constraints": "2.5.29.19",
    "key_usage": "2.5.29.15",
    "ext_key_usage": "2.5.29.37",
    "server_auth": "1.3.6.1.5.5.7.3.1",
}


def _name(common_name: str) -> bytes:
    return _seq(_set(_seq(_oid(_OID["common_name"]), _printable(common_name))))


def _general_name(host: str) -> bytes:
    try:
        return _tlv(0x87, ipaddress.ip_address(host).packed)  # iPAddress [7]
    except ValueError:
        return _tlv(0x82, host.encode("ascii"))  # dNSName [2]


def _extensions(hosts: Sequence[str]) -> bytes:
    san = _seq(_oid(_OID["san"]), _octets(_seq(*[_general_name(h) for h in hosts])))
    # basicConstraints CA:FALSE (critical)
    basic = _seq(_oid(_OID["basic_constraints"]), _bool(True), _octets(_seq()))
    # keyUsage: digitalSignature + keyEncipherment (critical)
    usage = _seq(_oid(_OID["key_usage"]), _bool(True), _octets(_tlv(0x03, b"\x05\xa0")))
    # extKeyUsage: serverAuth
    ext_usage = _seq(_oid(_OID["ext_key_usage"]), _octets(_seq(_oid(_OID["server_auth"]))))
    return _ctx(3, _seq(san, basic, usage, ext_usage))


def _pem(label: str, der: bytes) -> str:
    body = base64.encodebytes(der).decode("ascii")
    return f"-----BEGIN {label}-----\n{body}-----END {label}-----\n"


def _pkcs1v15_sign(key: dict[str, int], message: bytes) -> bytes:
    """RSASSA-PKCS1-v1.5 signature over SHA-256(``message``).

    This is exactly RS256 (RFC 7518 §3.3) — the same primitive that signs an X.509
    TBS below — so the ACME client (``servery._acme``) reuses it for JWS + the CSR.
    """
    digest = hashlib.sha256(message).digest()
    digest_info = _seq(_seq(_oid(_OID["sha256"]), _null()), _octets(digest))
    key_bytes = (key["n"].bit_length() + 7) // 8
    padding = b"\xff" * (key_bytes - len(digest_info) - 3)
    encoded = b"\x00\x01" + padding + b"\x00" + digest_info
    return pow(int.from_bytes(encoded, "big"), key["d"], key["n"]).to_bytes(key_bytes, "big")


def _rsa_private_key_pem(key: dict[str, int]) -> str:
    """Serialize an RSA key (the ``_generate_rsa`` dict) as a PKCS#1 ``RSA PRIVATE KEY``."""
    der = _seq(
        _int(0),
        _int(key["n"]),
        _int(key["e"]),
        _int(key["d"]),
        _int(key["p"]),
        _int(key["q"]),
        _int(key["dp"]),
        _int(key["dq"]),
        _int(key["qinv"]),
    )
    return _pem("RSA PRIVATE KEY", der)


def generate(
    hosts: Sequence[str] = ("localhost", "127.0.0.1", "::1"),
    *,
    days: int = 365,
    bits: int = 2048,
) -> tuple[str, str]:
    """Generate a self-signed cert for ``hosts``; return ``(cert_pem, key_pem)``.

    ``hosts`` becomes the SubjectAltName (DNS names and IP addresses are detected
    automatically); the first entry is also the subject Common Name.
    """
    if not hosts:
        raise ValueError("at least one host is required")
    key = _generate_rsa(bits)
    now = int(time.time())
    sig_alg = _seq(_oid(_OID["sha256_rsa"]), _null())
    spki = _seq(
        _seq(_oid(_OID["rsa"]), _null()),
        _bitstring(_seq(_int(key["n"]), _int(key["e"]))),
    )
    tbs = _seq(
        _ctx(0, _int(2)),  # version v3
        _int(secrets.randbits(64) | 1),  # serial number
        sig_alg,
        _name(hosts[0]),  # issuer == subject (self-signed)
        _seq(_utctime(now - 300), _utctime(now + days * 86400)),
        _name(hosts[0]),
        spki,
        _extensions(hosts),
    )
    certificate = _seq(tbs, sig_alg, _bitstring(_pkcs1v15_sign(key, tbs)))
    return _pem("CERTIFICATE", certificate), _rsa_private_key_pem(key)
