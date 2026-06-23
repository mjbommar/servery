# Design: zero-dependency ACME (Let's Encrypt) — `--acme`

Status: implemented (HTTP-01). Pure stdlib — no `cryptography`, no `openssl` shell-out.

## Why it's possible zero-dep
servery already hand-rolls, in `_certgen.py`, the exact primitives ACME needs:
RSA-2048 keygen, a DER encoder, and **PKCS#1 v1.5 SHA-256 signing** — which *is*
RS256 (RFC 7518 §3.3) and the same operation that signs a PKCS#10 CSR. So JWS
(RFC 7515) and the CSR are built from the existing toolkit; `_pkcs1v15_sign` is
factored out and reused. The only additions are base64url, the JWK/thumbprint, the
CSR assembly, and a urllib/JSON transport — all stdlib.

## Flow (RFC 8555, HTTP-01)
`_acme.AcmeClient.issue(domains, cert_key)`:
directory → newAccount (JWS with embedded **jwk**; account URL → **kid**) → newOrder
→ for each authorization: POST-as-GET it, serve the key authorization
(`token + "." + base64url(SHA-256(JWK thumbprint))`, RFC 7638/§8.1) at
`/.well-known/acme-challenge/<token>`, POST `{}` to the challenge, poll until
`valid` → finalize with `{"csr": base64url(DER)}` → poll order `valid` → download
the PEM chain. Nonces thread through every POST (fresh `Replay-Nonce` per response;
one retry on `badNonce`). POST-as-GET sends the **empty-string** payload (§6.3).

## Challenge serving
HTTP-01 must be answered over plain HTTP on the port the CA validates (80 in
production). `_acme.obtain()` runs a short-lived stdlib HTTP listener that serves
the key authorizations from an in-memory `{token: keyauth}` dict — **no files are
written into the served root**. (The main HTTP/1.1 handler could also answer the
path, but the dedicated listener keeps issuance self-contained and works even when
servery itself is serving HTTPS.)

## Keys, persistence, renewal
Separate **account key** (signs JWS, long-lived) and **certificate key** (in the
CSR/cert). Cached under `~/.config/servery/acme/<staging|production>/`:
`account.json` (skips re-registration — newAccount is idempotent, §7.3.1) and
`<domain>.crt`/`.key` (0600). On startup a cert younger than 60 days (LE certs last
90) is reused — never re-issue a still-valid cert (rate limits).

## CLI / config
`--acme DOMAIN` (repeatable), `--acme-email`, `--acme-production` (default is the
**staging** CA — safe). `serve()` obtains/loads the cert, then `dataclasses.replace`
sets `tls_cert`/`tls_key` and serves HTTPS as usual. Incompatible with
`--tls-cert`/`--tls-self-signed` (ACME provides the cert).

## Testing
- **Unit (CI default, no network):** JWK `e == "AQAB"`, RFC 7638 thumbprint
  (recomputed), CSR self-signature RSA-recovery + SAN presence, and the **full flow
  against an in-memory mock CA** (asserts the jwk→kid switch, nonce threading,
  POST-as-GET empty payload, `{}` to the challenge, `csr` in finalize, challenge
  provisioned then cleared).
- **Integration (opt-in, `SERVERY_ACME_PEBBLE=1` + Docker):** issue a real cert
  from **Pebble** and load it with `ssl.load_cert_chain`. Proven: Pebble accepts our
  JWS + PKCS#10 CSR and returns a valid 2-cert chain that `ssl` loads with the key.
- **Manual e2e:** Let's Encrypt **staging** (real domain reachable on :80). Never
  production in tests (rate limits).
