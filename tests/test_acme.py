"""ACME client tests: JWK/thumbprint/CSR primitives + the full flow against a mock CA."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shutil
import ssl
import subprocess  # nosec B404 - manages the Pebble test container (opt-in integration test)
import tempfile
import time
import unittest
import urllib.request

from servery import _acme, _certgen

_B = "https://acme.test"


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _drop(store: dict[str, str], token: str) -> None:  # a None-returning clear_challenge
    store.pop(token, None)


def _read_tlv(data: bytes, offset: int) -> tuple[int, int, int]:
    """Return (tag, content_start, content_end) for the DER element at ``offset``."""
    tag = data[offset]
    length = data[offset + 1]
    pos = offset + 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos : pos + n], "big")
        pos += n
    return tag, pos, pos + length


class PrimitiveTest(unittest.TestCase):
    def setUp(self):
        self.key = _certgen._generate_rsa(2048)

    def test_jwk_exponent_is_aqab(self):
        self.assertEqual(_acme.jwk(self.key)["e"], "AQAB")  # 65537 -> AQAB (RFC 7518 §6.3.1.2)
        self.assertEqual(_acme.jwk(self.key)["kty"], "RSA")

    def test_thumbprint_matches_manual_rfc7638(self):
        canonical = json.dumps(_acme.jwk(self.key), sort_keys=True, separators=(",", ":"))
        expected = base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(
            b"="
        )
        self.assertEqual(_acme.thumbprint(self.key), expected.decode())
        self.assertIn('"kty":"RSA"', canonical)  # compact, sorted (e, kty, n)

    def test_key_authorization_format(self):
        ka = _acme.key_authorization("a-token", self.key)
        token, _, thumb = ka.partition(".")
        self.assertEqual(token, "a-token")
        self.assertEqual(thumb, _acme.thumbprint(self.key))

    def test_csr_structure_and_san(self):
        csr = _acme.build_csr(self.key, ["example.com", "www.example.com"])
        self.assertEqual(csr[0], 0x30)  # SEQUENCE
        self.assertIn(b"example.com", csr)
        self.assertIn(b"www.example.com", csr)

    def test_csr_self_signature_verifies(self):
        # The CSR signs its CertificationRequestInfo (the first inner element) with
        # the cert key — verify by RSA-recovering the PKCS#1 v1.5 padded digest.
        domains = ["servery.example"]
        csr = _acme.build_csr(self.key, domains)
        _tag, body, _end = _read_tlv(csr, 0)  # outer SEQUENCE
        _cri_tag, _cri_body, cri_end = _read_tlv(csr, body)  # CertificationRequestInfo
        cri = csr[body:cri_end]
        # skip the signatureAlgorithm SEQUENCE, then read the signature BIT STRING
        _sa_tag, _sa_body, sa_end = _read_tlv(csr, cri_end)
        sig_tag, sig_body, sig_end = _read_tlv(csr, sa_end)
        self.assertEqual(sig_tag, 0x03)  # BIT STRING
        signature = csr[sig_body + 1 : sig_end]  # drop the unused-bits octet
        recovered = pow(int.from_bytes(signature, "big"), self.key["e"], self.key["n"])
        em = recovered.to_bytes((self.key["n"].bit_length() + 7) // 8, "big")
        self.assertTrue(em.startswith(b"\x00\x01\xff"))
        self.assertIn(hashlib.sha256(cri).digest(), em)  # the digest is embedded in the padding


class _MockCA:
    """A minimal in-memory ACME server for exercising the client's flow."""

    def __init__(self) -> None:
        self.nonce = 0
        self.authz_fetches = 0
        self.posts: list[tuple[str, dict, str]] = []

    def _headers(self, **extra: str) -> dict[str, str]:
        self.nonce += 1
        return {"Replay-Nonce": f"nonce-{self.nonce}", **extra}

    def open(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        if method == "HEAD":
            return _Resp(200, b"", self._headers())
        if url == _B + "/dir":
            body = {
                "newNonce": _B + "/nonce",
                "newAccount": _B + "/acct",
                "newOrder": _B + "/order",
            }
            return _Resp(200, json.dumps(body), self._headers())
        env = json.loads(req.data)
        protected = json.loads(_b64u_decode(env["protected"]))
        payload = env["payload"]
        self.posts.append((url, protected, payload))
        return self._route(url, payload)

    def _route(self, url: str, payload: str):
        if url == _B + "/acct":
            return _Resp(
                201, json.dumps({"status": "valid"}), self._headers(Location=_B + "/acct/1")
            )
        if url == _B + "/order":
            order = {
                "status": "pending",
                "authorizations": [_B + "/authz/1"],
                "finalize": _B + "/finalize",
            }
            return _Resp(201, json.dumps(order), self._headers(Location=_B + "/order/1"))
        if url == _B + "/authz/1":
            self.authz_fetches += 1
            if self.authz_fetches == 1:
                authz = {
                    "status": "pending",
                    "identifier": {"type": "dns", "value": "example.com"},
                    "challenges": [{"type": "http-01", "token": "tok", "url": _B + "/chall/1"}],
                }
                return _Resp(200, json.dumps(authz), self._headers())
            return _Resp(200, json.dumps({"status": "valid"}), self._headers())
        if url == _B + "/chall/1":
            return _Resp(200, json.dumps({"status": "processing"}), self._headers())
        if url == _B + "/finalize":
            return _Resp(200, json.dumps({"status": "processing"}), self._headers())
        if url == _B + "/order/1":
            valid = {"status": "valid", "certificate": _B + "/cert/1"}
            return _Resp(200, json.dumps(valid), self._headers())
        if url == _B + "/cert/1":
            return _Resp(
                200,
                "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n",
                self._headers(),
            )
        return _Resp(404, b"", self._headers())


class _Resp:
    def __init__(self, status, body, headers):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FlowTest(unittest.TestCase):
    def test_full_http01_flow_against_mock(self):
        ca = _MockCA()
        challenges: dict[str, str] = {}
        account = _certgen._generate_rsa(2048)
        cert_key = _certgen._generate_rsa(2048)
        client = _acme.AcmeClient(
            _B + "/dir",
            account,
            set_challenge=challenges.__setitem__,
            clear_challenge=lambda t: _drop(challenges, t),
        )
        client._opener = ca  # ty: ignore[invalid-assignment]  # inject the mock transport

        pem = client.issue(["example.com"], cert_key)
        self.assertIn("BEGIN CERTIFICATE", pem)

        # First signed POST (newAccount) embeds the JWK; everything after uses the kid.
        self.assertIn("jwk", ca.posts[0][1])
        self.assertNotIn("kid", ca.posts[0][1])
        self.assertTrue(all(p[1].get("kid") == _B + "/acct/1" for p in ca.posts[1:]))
        # Nonce threads forward (each POST uses a distinct, increasing nonce).
        nonces = [p[1]["nonce"] for p in ca.posts]
        self.assertEqual(len(nonces), len(set(nonces)))
        # POST-as-GET uses an empty payload; the challenge poke is {}; finalize has a csr.
        chall = next(p for p in ca.posts if p[0] == _B + "/chall/1")
        self.assertEqual(json.loads(_b64u_decode(chall[2])), {})
        authz_get = next(p for p in ca.posts if p[0] == _B + "/authz/1")
        self.assertEqual(authz_get[2], "")  # POST-as-GET
        finalize = next(p for p in ca.posts if p[0] == _B + "/finalize")
        self.assertIn("csr", json.loads(_b64u_decode(finalize[2])))
        # The challenge was provisioned during validation and cleared afterward.
        self.assertEqual(challenges, {})


@unittest.skipUnless(
    shutil.which("docker") and os.environ.get("SERVERY_ACME_PEBBLE"),
    "set SERVERY_ACME_PEBBLE=1 (with Docker) to run the Pebble integration test",
)
class PebbleIntegrationTest(unittest.TestCase):
    """Issue a real certificate from Pebble — proves the JWS + CSR are CA-accepted."""

    _NAME = "servery-pebble-test"

    def setUp(self):
        subprocess.run(["docker", "rm", "-f", self._NAME], capture_output=True, check=False)  # nosec B607
        subprocess.run(
            [  # nosec B607
                "docker",
                "run",
                "-d",
                "--name",
                self._NAME,
                "-e",
                "PEBBLE_VA_ALWAYS_VALID=1",
                "-p",
                "14000:14000",
                "-p",
                "15000:15000",
                "ghcr.io/letsencrypt/pebble:latest",
            ],
            check=True,
            capture_output=True,
        )
        self.ca = str(pathlib.Path(tempfile.mkdtemp()) / "pebble.minica.pem")
        self.addCleanup(
            lambda: subprocess.run(
                ["docker", "rm", "-f", self._NAME], capture_output=True, check=False
            )
        )  # nosec B607
        for _ in range(60):
            if (
                subprocess.run(  # nosec B607
                    ["docker", "cp", f"{self._NAME}:/test/certs/pebble.minica.pem", self.ca],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.5)
        ctx = ssl.create_default_context(cafile=self.ca)
        for _ in range(60):
            try:
                urllib.request.urlopen("https://localhost:14000/dir", context=ctx, timeout=2)
            except OSError:
                time.sleep(0.5)
            else:
                return
        self.skipTest("Pebble did not become ready")

    def test_issue_real_certificate(self):
        challenges: dict[str, str] = {}
        client = _acme.AcmeClient(
            "https://localhost:14000/dir",
            _certgen._generate_rsa(2048),
            ca_bundle=self.ca,
            set_challenge=challenges.__setitem__,
            clear_challenge=lambda t: _drop(challenges, t),
        )
        cert_key = _certgen._generate_rsa(2048)
        pem = client.issue(["servery.example"], cert_key)
        self.assertEqual(pem.count("BEGIN CERTIFICATE"), 2)  # leaf + issuer
        # The issued cert and our key load together — they're a valid, matched pair.
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "c").write_text(pem)
        (d / "k").write_text(_certgen._rsa_private_key_pem(cert_key))
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(d / "c", d / "k")


if __name__ == "__main__":
    unittest.main()
