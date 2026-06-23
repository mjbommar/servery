"""Pure-stdlib ACME v2 client (RFC 8555) — automatic Let's Encrypt certificates.

The hard part is normally the crypto, but servery already hand-rolls RSA + DER +
PKCS#1 v1.5 signing in :mod:`servery._certgen`, so JWS (RFC 7515) and the PKCS#10
CSR need **no** third-party package. HTTP-01 only: the key authorization is served
from memory by the HTTP/1.1 handler (no files written into the served root).

This module speaks the protocol; persistence, renewal, and challenge serving are
wired in by the caller (see ``server.py`` / ``config.py``). Test against Pebble or
the Let's Encrypt staging endpoint — never iterate against production (rate limits).
"""

from __future__ import annotations

import hashlib
import json
import ssl
import time
import urllib.error
import urllib.request
from base64 import urlsafe_b64encode
from collections.abc import Callable
from typing import Any

from servery import _certgen, _log

LE_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"

_USER_AGENT = "servery-acme/1.0"


class AcmeError(RuntimeError):
    """An ACME protocol error (a CA problem document, or a flow failure)."""


def _b64u(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64u(value: int) -> str:
    # RFC 7518 Base64urlUInt: minimal big-endian octets, no leading zero pad.
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8 or 1, "big"))


def jwk(key: dict[str, int]) -> dict[str, str]:
    """The public JWK for an RSA key (RFC 7517/7518 §6.3) — members in JWK order."""
    return {"e": _int_b64u(key["e"]), "kty": "RSA", "n": _int_b64u(key["n"])}


def thumbprint(key: dict[str, int]) -> str:
    """RFC 7638 JWK thumbprint: SHA-256 of the canonical (sorted, compact) JWK."""
    canonical = json.dumps(jwk(key), sort_keys=True, separators=(",", ":"))
    return _b64u(hashlib.sha256(canonical.encode("utf-8")).digest())


def key_authorization(token: str, account_key: dict[str, int]) -> str:
    """The HTTP-01 key authorization served at the challenge path (RFC 8555 §8.1)."""
    return f"{token}.{thumbprint(account_key)}"


def build_csr(cert_key: dict[str, int], domains: list[str]) -> bytes:
    """A signed PKCS#10 CSR (DER) for ``domains`` with a subjectAltName (RFC 2986)."""
    c = _certgen
    spki = c._seq(
        c._seq(c._oid(c._OID["rsa"]), c._null()),
        c._bitstring(c._seq(c._int(cert_key["n"]), c._int(cert_key["e"]))),
    )
    san = c._seq(
        c._oid(c._OID["san"]),
        c._octets(c._seq(*[c._general_name(d) for d in domains])),
    )
    # attributes [0] IMPLICIT: one extensionRequest carrying the SAN extension.
    attributes = c._ctx(0, c._seq(c._oid(c._OID["ext_request"]), c._set(c._seq(san))))
    request_info = c._seq(c._int(0), c._name(domains[0]), spki, attributes)
    sig_alg = c._seq(c._oid(c._OID["sha256_rsa"]), c._null())
    return c._seq(request_info, sig_alg, c._bitstring(c._pkcs1v15_sign(cert_key, request_info)))


class AcmeClient:
    """A minimal RFC 8555 client for the HTTP-01 happy path."""

    def __init__(
        self,
        directory_url: str,
        account_key: dict[str, int],
        *,
        contact: str | None = None,
        ca_bundle: str | None = None,
        set_challenge: Callable[[str, str], None],
        clear_challenge: Callable[[str], None],
    ) -> None:
        self._directory_url = directory_url
        self._account_key = account_key
        self._contact = contact
        self._jwk = jwk(account_key)
        self._set_challenge = set_challenge
        self._clear_challenge = clear_challenge
        self._kid: str | None = None
        self._nonce: str | None = None
        self._dir: dict[str, str] = {}
        context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None
        handler = urllib.request.HTTPSHandler(context=context) if context else None
        self._opener = urllib.request.build_opener(*([handler] if handler else []))

    # --- transport -------------------------------------------------------

    def _raw(self, request: urllib.request.Request) -> tuple[int, bytes, Any]:
        try:
            with self._opener.open(request, timeout=30) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:  # ACME errors carry a JSON problem + a nonce
            return exc.code, exc.read(), exc.headers

    def _get(self, url: str) -> tuple[Any, Any]:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        status, body, headers = self._raw(req)
        if headers.get("Replay-Nonce"):
            self._nonce = headers["Replay-Nonce"]
        if status >= 400:
            raise AcmeError(f"GET {url} -> {status}: {body[:200]!r}")
        return (json.loads(body) if body else None), headers

    def _new_nonce(self) -> str:
        req = urllib.request.Request(
            self._dir["newNonce"], method="HEAD", headers={"User-Agent": _USER_AGENT}
        )
        _status, _body, headers = self._raw(req)
        return headers["Replay-Nonce"]

    def _post(self, url: str, payload: Any) -> tuple[Any, Any]:
        """Signed POST. ``payload`` None => POST-as-GET (empty payload, §6.3)."""
        for attempt in range(2):  # one retry on a bad nonce
            if not self._nonce:
                self._nonce = self._new_nonce()
            protected: dict[str, Any] = {"alg": "RS256", "nonce": self._nonce, "url": url}
            if self._kid is not None:
                protected["kid"] = self._kid
            else:
                protected["jwk"] = self._jwk
            protected_b64 = _b64u(json.dumps(protected).encode())
            payload_b64 = "" if payload is None else _b64u(json.dumps(payload).encode())
            signature = _certgen._pkcs1v15_sign(
                self._account_key, f"{protected_b64}.{payload_b64}".encode()
            )
            body = json.dumps(
                {"protected": protected_b64, "payload": payload_b64, "signature": _b64u(signature)}
            ).encode()
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/jose+json", "User-Agent": _USER_AGENT},
            )
            status, raw, headers = self._raw(req)
            self._nonce = headers.get("Replay-Nonce")
            parsed = json.loads(raw) if raw and raw[:1] in (b"{", b"[") else raw
            if status >= 400:
                kind = parsed.get("type", "") if isinstance(parsed, dict) else ""
                if attempt == 0 and kind.endswith("badNonce"):
                    continue  # retry with the fresh nonce from this response
                raise AcmeError(f"POST {url} -> {status}: {parsed}")
            return parsed, headers
        raise AcmeError(f"POST {url}: exhausted nonce retries")  # pragma: no cover

    # --- flow ------------------------------------------------------------

    def _load_directory(self) -> None:
        self._dir, _ = self._get(self._directory_url)

    def register(self) -> None:
        payload: dict[str, Any] = {"termsOfServiceAgreed": True}
        if self._contact:
            payload["contact"] = [f"mailto:{self._contact}"]
        _account, headers = self._post(self._dir["newAccount"], payload)
        self._kid = headers["Location"]  # account URL -> kid for every later POST

    def _poll(self, url: str, *, pending: set[str]) -> dict[str, Any]:
        deadline = time.monotonic() + 120
        while True:
            obj, headers = self._post(url, None)  # POST-as-GET
            status = obj.get("status")
            if status not in pending:
                return obj
            if time.monotonic() > deadline:
                raise AcmeError(f"timed out polling {url} (status {status})")
            try:
                delay = min(10, max(1, int(headers.get("Retry-After", "2"))))
            except ValueError:
                delay = 2
            time.sleep(delay)

    def _authorize(self, authz_url: str) -> None:
        authz, _ = self._post(authz_url, None)
        if authz.get("status") == "valid":
            return
        challenge = next(c for c in authz["challenges"] if c["type"] == "http-01")
        token = challenge["token"]
        self._set_challenge(token, key_authorization(token, self._account_key))
        try:
            self._post(challenge["url"], {})  # tell the CA we're ready
            result = self._poll(authz_url, pending={"pending", "processing"})
            if result.get("status") != "valid":
                raise AcmeError(f"authorization for {authz.get('identifier')} failed: {result}")
        finally:
            self._clear_challenge(token)

    def issue(self, domains: list[str], cert_key: dict[str, int]) -> str:
        """Run the full HTTP-01 flow; return the issued PEM certificate chain."""
        self._load_directory()
        self.register()
        order, headers = self._post(
            self._dir["newOrder"],
            {"identifiers": [{"type": "dns", "value": d} for d in domains]},
        )
        order_url = headers["Location"]
        for authz_url in order["authorizations"]:
            self._authorize(authz_url)
        self._post(order["finalize"], {"csr": _b64u(build_csr(cert_key, domains))})
        order = self._poll(order_url, pending={"pending", "ready", "processing"})
        if order.get("status") != "valid":
            raise AcmeError(f"order did not become valid: {order}")
        chain, _ = self._get_cert(order["certificate"])
        _log.logger.info("ACME: issued certificate for %s", ", ".join(domains))
        return chain

    def _get_cert(self, cert_url: str) -> tuple[str, Any]:
        # POST-as-GET; the body is the PEM chain (not JSON).
        chain, headers = self._post(cert_url, None)
        return (chain.decode("ascii") if isinstance(chain, bytes) else chain), headers


_CHALLENGE_PREFIX = "/.well-known/acme-challenge/"


def _challenge_server(port: int, tokens: dict[str, str]) -> Any:
    """A tiny HTTP server that serves the HTTP-01 key authorizations from ``tokens``."""
    import http.server

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = ""
            if self.path.startswith(_CHALLENGE_PREFIX):
                body = tokens.get(self.path[len(_CHALLENGE_PREFIX) :], "")
            payload = body.encode()
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # quiet

    return http.server.HTTPServer(("", port), _Handler)  # nosec B104 - HTTP-01 needs all interfaces


def obtain(  # pragma: no cover - integration-only (live CA + bound listener; see Pebble test)
    domains: list[str],
    *,
    email: str | None = None,
    directory_url: str = LE_STAGING,
    ca_bundle: str | None = None,
    http_port: int = 80,
    account_key: dict[str, int] | None = None,
    cert_key: dict[str, int] | None = None,
) -> tuple[str, str]:
    """Obtain a certificate via HTTP-01; return ``(cert_chain_pem, key_pem)``.

    Spins a short-lived HTTP listener on ``http_port`` (must be reachable by the CA
    — port 80 in production) to answer the challenge. Keys are generated if not given.

    Orchestrates over a live CA + a bound listener — exercised by the opt-in Pebble
    integration test (tests/test_acme.py), not the default unit suite.
    """
    import threading

    account_key = account_key or _certgen._generate_rsa(2048)
    cert_key = cert_key or _certgen._generate_rsa(2048)
    tokens: dict[str, str] = {}

    def set_challenge(token: str, auth: str) -> None:
        tokens[token] = auth

    def clear_challenge(token: str) -> None:
        tokens.pop(token, None)

    httpd = _challenge_server(http_port, tokens)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        client = AcmeClient(
            directory_url,
            account_key,
            contact=email,
            ca_bundle=ca_bundle,
            set_challenge=set_challenge,
            clear_challenge=clear_challenge,
        )
        chain = client.issue(domains, cert_key)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
    return chain, _certgen._rsa_private_key_pem(cert_key)
