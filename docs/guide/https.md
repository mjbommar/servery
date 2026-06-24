# HTTPS & certificates

servery speaks TLS three ways, from "instant and ad-hoc" to "real, browser-trusted,
automatic" — all with **no third-party dependencies**.

## Instant: a self-signed certificate

```bash
servery --tls-self-signed
```

servery generates an RSA key + self-signed certificate **at startup** (pure stdlib —
no `openssl`, no `cryptography`) and serves HTTPS immediately. Clients see an
"untrusted certificate" warning, which is fine for a dev box or a quick encrypted
LAN share. ALPN and HSTS are set over TLS.

## Bring your own certificate

```bash
servery --tls-cert fullchain.pem --tls-key privkey.pem
```

Point servery at an existing PEM certificate chain and private key. If the key is
encrypted, supply the passphrase via a file:

```bash
servery --tls-cert cert.pem --tls-key key.pem --tls-password-file ./key.pass
```

`servery --tls-help` prints a one-liner for generating a self-signed cert with
`openssl`, if you'd rather manage it yourself.

## Automatic: Let's Encrypt via ACME

If a domain points at your machine and the **HTTP-01 challenge is reachable on
port 80**, servery can obtain a real, browser-trusted certificate automatically:

```bash
servery --acme example.com --acme-email you@example.com
```

This runs the full **ACME v2** flow (RFC 8555) — account registration, HTTP-01
challenge, CSR, certificate download — built entirely on servery's own stdlib RSA +
DER + PKCS#1 signing. **Almost no other tool offers trusted auto-TLS with zero
dependencies.**

| Flag | Default | Meaning |
| --- | --- | --- |
| `--acme DOMAIN` | — | obtain a cert for DOMAIN (repeatable for multiple names) |
| `--acme-email EMAIL` | — | ACME account contact |
| `--acme-production` | **staging** | use the real Let's Encrypt CA |

!!! tip "Staging first"

    `--acme` defaults to the Let's Encrypt **staging** CA so you can test without
    hitting rate limits — the cert won't be browser-trusted, but the whole flow is
    exercised. Add `--acme-production` once it works.

The account key and certificate are cached under `~/.config/servery/acme/`, and a
still-valid certificate is reused on restart (so you don't re-issue and risk rate
limits). Multiple `--acme` flags request a single certificate covering all the names.

```bash
# Production HTTPS for two names, with HTTP/2:
sudo servery --acme example.com --acme www.example.com \
     --acme-email you@example.com --acme-production \
     --bind 0.0.0.0 --port 443 --http2
```

(`sudo` / a privileged port is needed to bind `:443` and answer the `:80`
challenge.)

## See also

- [Authentication](uploads.md#requiring-a-password) — pair TLS with `--auth`.
- [HTTP/2 & HTTP/3](protocols.md) — both run over TLS.
