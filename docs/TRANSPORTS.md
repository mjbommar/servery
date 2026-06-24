# Transports

> Companion to `VISION.md`, `PRINCIPLES.md`, `STANDARDS.md`, and
> `ARCHITECTURE.md`. This document records servery's **multi-HTTP-version
> transport strategy** as a finalized architecture decision: which HTTP versions
> servery speaks, by what mechanism, with what crypto, at what cost, and under
> what install/runtime conditions.
>
> **Supreme constraint, unchanged (`PRINCIPLES.md` §0):** the servery **core** has
> **zero third-party (PyPI) runtime dependencies** — `pip install servery` pulls in
> nothing but servery and the standard library, forever. Everything in this
> document is built *around* that constraint, never against it. Where a higher HTTP
> version needs more than the stdlib, the answer is an **optional, opt-in tier** —
> first preferring **already-present OS libraries bound via `ctypes`** (stdlib) over
> any PyPI dependency, and only then a **vetted PyPI extra** the user explicitly
> asks for. The core never carries that weight.

RFC citations are to primary source text (read directly): RFC 9113 (HTTP/2),
RFC 7541 (HPACK), RFC 9114 (HTTP/3), RFC 9204 (QPACK), RFC 9000/9001 (QUIC +
QUIC-TLS), RFC 8446 (TLS 1.3), RFC 5869 (HKDF). CVE identifiers are the
canonical NVD/CVE.org records.

---

## 1. The decision, in one paragraph

servery speaks **HTTP/1.1 in its pure-stdlib core** (the product), and offers
**HTTP/2 and HTTP/3 as optional, opt-in transport tiers** that never touch the
zero-dependency core. HTTP/2 is **feasible in pure stdlib** (TLS+ALPN is already
`ssl`; HPACK and binary framing are pure data + code) and is therefore the
*preferred* path, with the mature `h2` library available as an optional backend
for those who want a battle-tested implementation. HTTP/3 **cannot** be done in
pure stdlib (QUIC needs AEAD ciphers the stdlib does not ship), so it is an
optional tier with two backends: the pragmatic, supported **`aioquic`** PyPI
extra, and an **experimental, system-gated `ctypes`→OpenSSL ≥ 3.5 native backend**
that achieves HTTP/3 with **zero PyPI dependencies** by binding crypto/QUIC that
is already on the machine. This reverses the old "HTTP/2 and HTTP/3 are
permanently out" stance (`STANDARDS.md` §1.2) — they are no longer out, they are
**tiered**.

---

## 2. The tiered transport model

Each row is a *transport tier*. The core is Tier 0; everything else is an
explicit, opt-in extra that the core can live without.

| Tier | Protocol | Wire mechanism | TLS / handshake | Crypto source | Install | Runtime requirements | Status |
|------|----------|----------------|-----------------|---------------|---------|----------------------|--------|
| **0 — Core** | **HTTP/1.1** (RFC 9110/9111/9112) | Text framing via stdlib `http.server` + `socketserver` | stdlib `ssl` (`SSLContext`); ALPN advertises **`http/1.1`** | **stdlib only** (`ssl`/OpenSSL via the interpreter) | `pip install servery` — **zero deps** | CPython ≥ 3.13; nothing else | **Shipped / the product** |
| **1 — HTTP/2 (stdlib)** | **HTTP/2** (RFC 9113) | Hand-rolled binary framing + HPACK (RFC 7541) + stream state machine + flow control | stdlib `ssl`; ALPN adds **`h2`** (negotiated; falls back to `http/1.1`) | **stdlib only** — TLS/ALPN from `ssl`; HPACK incl. its Huffman table is pure data; no extra crypto | `pip install servery` (built-in, gated by a flag) | CPython ≥ 3.13; OpenSSL with ALPN (already required by `ssl`) | **Preferred h2 path — to build** |
| **1′ — HTTP/2 (`h2` lib)** | **HTTP/2** (RFC 9113) | `h2` (python-hyper) drives framing/HPACK/state; servery owns sockets + I/O | stdlib `ssl`; ALPN adds **`h2`** | stdlib `ssl` for TLS; `h2` is pure-Python framing/HPACK (no crypto) | `pip install servery[http2]` | `h2` (deps `hpack` + `hyperframe`; or the `jh2` fork, vendored, optional Rust HPACK) | **Optional backend** |
| **2 — HTTP/3 (`aioquic`)** | **HTTP/3** (RFC 9114) over QUIC (RFC 9000) | `aioquic` provides QUIC + QPACK + h3; servery binds UDP + drives the loop | TLS 1.3-in-QUIC via `aioquic`; ALPN **`h3`** | `cryptography` (OpenSSL AEADs) via `aioquic` | `pip install servery[http3]` | `aioquic` (deps `cryptography` + `pylsqpack`); UDP reachable | **Supported h3 path** |
| **3 — HTTP/3 (native, `ctypes`)** | **HTTP/3** (RFC 9114) over QUIC | `ctypes`→ system OpenSSL ≥ 3.5 QUIC server + QUIC-TLS API; servery owns h3/QPACK glue | TLS 1.3 handshake ridden in QUIC frames via OpenSSL's external QUIC-TLS record layer | **OS-provided** — system `libssl`/`libcrypto` (already loaded), bound via `ctypes`; HKDF is stdlib `hmac` | `pip install servery` — **zero PyPI deps**, but system-gated | OpenSSL **≥ 3.5** present on the host (Linux/macOS); UDP reachable; you own the FFI | **Experimental / advanced / system-dependent** |

**How to read this table.** Tiers 0, 1, and 3 add **no PyPI dependency**. Tiers 1′
and 2 are **opt-in PyPI extras** for users who prefer a mature, externally
maintained implementation over servery's own (h2) or who lack OpenSSL 3.5 (h3).
The *default* `servery` install can speak HTTP/1.1 always, HTTP/2 when the
built-in stdlib backend is enabled, and HTTP/3 **only** if the host happens to
carry OpenSSL ≥ 3.5 and the experimental native backend is opted into.

### Why each tier sits where it does

- **HTTP/2 needs no extra crypto.** TLS and ALPN (`'h2'`) are already handled by
  stdlib `ssl`. HPACK (RFC 7541), including its static Huffman table, is pure data
  plus a decoder; the binary framing (RFC 9113 §4/§6) is pure code. So a
  pure-stdlib HTTP/2 server is genuinely feasible — the cost is **size** (~2–4k
  LOC for framing + HPACK + stream state machine + flow control) and a real
  **security burden** (§6). This is why Tier 1 is *preferred* but *flagged* and
  not yet the core default.
- **HTTP/3 genuinely cannot be pure stdlib.** QUIC (RFC 9001) protects packets
  with AEAD ciphers — AES-128-GCM and ChaCha20-Poly1305 — plus HKDF (RFC 5869),
  and drives a TLS 1.3 handshake whose bytes ride *inside* QUIC frames. **The
  stdlib ships no symmetric ciphers at all**, and `ssl` cannot be driven as a
  raw handshake-bytes engine for QUIC. That is a hard wall for *pure* stdlib —
  but a wall that is **tunnelable via `ctypes`** to crypto already on the box
  (§4), which is exactly what Tier 3 does.

---

## 3. The pluggable transport-backend seam

servery should select a transport at runtime behind a **small, stable
abstraction** — enough of a seam that h2/h3 backends slot in without the core
growing a framework, and no more. Do **not** over-specify this; sketch it and let
the implementing PR settle details.

### 3.1 The shape

A transport backend is the thing that, given a freshly accepted connection (TCP
stream for h1/h2; UDP/QUIC association for h3) and servery's `Config`, turns wire
bytes into servery's existing request-handling and back. The seam is a narrow
`Protocol`/ABC, roughly:

```python
from typing import Protocol

class TransportBackend(Protocol):
    #: ALPN id this backend speaks ("http/1.1", "h2", "h3").
    alpn_id: str

    def available(self, config) -> bool:
        """Can this backend run here? (import present, OS lib present, OpenSSL>=3.5, ...)"""

    def serve_connection(self, conn, config) -> None:
        """Own one connection: frame/demux requests, dispatch each into servery's
        handler pipeline (send_head/do_POST), and frame responses back out."""
```

Backends register by ALPN id. The **request-handling core does not change**: each
backend ultimately calls into the same `send_head` / `do_POST` / listing / range /
auth pipeline (`ARCHITECTURE.md` §3). A backend owns *transport* (framing,
multiplexing, flow control), never *file-serving policy* — that stays in the one
handler. This keeps Principle 2 intact: adding h2/h3 is adding a transport, not a
framework surface.

### 3.2 Selection / negotiation

| Path | How a backend is chosen |
|------|-------------------------|
| **HTTP/1.1 (Tier 0)** | Always available; the floor. Over TLS, `SSLContext` advertises `http/1.1`. |
| **HTTP/2 over TLS** | **ALPN negotiation.** When h2 is enabled, the `SSLContext` advertises `["h2", "http/1.1"]`; the client's ALPN pick selects the backend. No client pick / no h2 → **graceful fallback to h1.1** on the same socket. (h2 cleartext "h2c" prior-knowledge/Upgrade is explicitly *not* a goal; servery is TLS-first.) |
| **HTTP/3 (Tier 2/3)** | **Not** ALPN-on-the-TCP-socket — h3 is a *separate UDP listener*. Enabled by explicit opt-in (`--http3`) or auto-detect (Tier 3 only if OpenSSL ≥ 3.5 is found *and* opted in). When h3 is live, h1.1/h2 responses advertise it via **`Alt-Svc: h3=":443"`** so clients can upgrade on a later connection; the TCP path remains the fallback. |
| **Backend unavailable** | `available()` is false (missing extra, no OpenSSL 3.5, no UDP) → servery **logs once and falls back** to the next-lower tier it can speak. Never a hard failure when a lower tier exists. |

The invariant, restated from `STANDARDS.md`: **servery MUST NOT advertise (via
ALPN or `Alt-Svc`) a protocol it cannot actually speak in the current build.** The
core advertises only `http/1.1`; `h2`/`h3` are advertised **only** when the
corresponding backend is present *and* enabled.

### 3.3 Concurrency note

HTTP/2 and HTTP/3 multiplex **many logical streams over one connection**, which
sits awkwardly on the core's one-thread-per-connection model (`ARCHITECTURE.md`
§6). A multiplexing backend should either drive its connection with an internal
event loop (the natural fit for `aioquic`, which is asyncio-native) or a careful
threaded design with explicit per-stream state — owned *inside* the backend, not
leaked into the core handler. This is also why **free-threading is a first-class
target** (`PRINCIPLES.md`): a multiplexed h2 backend must hold no module-level
mutable state and must not lean on the GIL for stream-table safety; it has to be
correct on 3.13t/3.14t.

---

## 4. Crypto-sourcing policy

This is the policy that makes a **zero-PyPI HTTP/3** even conceivable, and it
applies to any future crypto need. Prefer sources in this strict order:

1. **Standard library.** Use it whenever it suffices. HKDF (RFC 5869) is **pure
   HMAC** and needs nothing beyond stdlib `hmac`/`hashlib` — no ctypes at all.
   TLS for h1/h2 is stdlib `ssl`. Hashes are `hashlib`. Tokens/nonces for
   non-crypto-construction uses are `secrets`. The stdlib is the first answer.
2. **OS-provided crypto, bound via `ctypes` (stdlib).** When the stdlib lacks a
   primitive (symmetric AEAD for QUIC: AES-128-GCM, ChaCha20-Poly1305), bind the
   crypto **already present and already loaded in-process**:
   - **Linux / macOS:** OpenSSL **`libcrypto`** — the same library that backs
     `hashlib` and `ssl`, so it is in the process already. Its EVP AEAD interface
     (`EVP_aead_*` / `EVP_CIPHER` GCM/ChaCha20-Poly1305) is callable via `ctypes`
     with **no compilation**. On a host with OpenSSL **≥ 3.5**, `libssl` also
     exposes a full **QUIC server** and the **external QUIC-TLS API**, so *both*
     the QUIC transport and the TLS-1.3 handshake-as-bytes can come from one
     system library, zero PyPI.
   - **Windows:** **CNG / `bcrypt.dll`** provides both AEADs (AES-GCM and
     ChaCha20-Poly1305 on Windows 11 / Server 2022+), callable via `ctypes`.
   **Always bind the vetted *high-level* AEAD** (one-shot seal/open over the EVP
   or CNG AEAD API). **Never hand-roll a primitive** — no home-grown GCM, no
   home-grown ChaCha20, no custom block-cipher mode. We are *binding* audited
   crypto, not *writing* crypto.
3. **PyPI crypto, only as an explicit optional extra.** `cryptography` (via
   `aioquic`, Tier 2) is the supported path for users who cannot or will not rely
   on system OpenSSL ≥ 3.5. It is **never** a core dependency and is pulled in only
   by `pip install servery[http3]`.

### The honest risk note (we own these, on purpose)

- **You own the FFI boundary.** `ctypes` into `libcrypto`/`libssl`/`bcrypt` means
  servery is responsible for correct argument types, buffer sizing, lifetimes,
  and error checking. A `ctypes` mistake is a memory-safety / correctness bug, not
  a clean Python exception. This code must be small, isolated, and heavily tested.
- **You own crypto correctness, not just calls.** Even using a high-level AEAD,
  servery owns **nonce construction** (QUIC packet-number-derived nonces MUST NOT
  repeat under a key — RFC 9001 §5.3), **tag handling**, key-update timing, and
  header protection. Getting nonce uniqueness wrong is catastrophic. This is the
  single sharpest reason Tier 3 is **experimental**.
- **OS / version availability varies.** OpenSSL **3.5** (April 2025, LTS, ~5-year
  support) is the floor for the native QUIC path — but adoption lags: Ubuntu 24.04
  ships OpenSSL **3.0**; only newer distros carry 3.5. macOS and Windows diverge
  further (Windows would use MsQuic/Schannel rather than OpenSSL). So Tier 3 is
  **legitimately useful but not the everywhere-default**; `available()` gates it
  hard and we fall back to Tier 2 or a TCP tier when the system lib is absent.
- **The boundary stays thin.** All `ctypes`/AEAD code lives behind the §3 backend
  seam in its own module(s), reviewable in isolation, exactly like `security.py`
  is for path safety (`ARCHITECTURE.md` §5).

### 4.1 TLS / HTTPS certificate tiers (parallel to the transport tiers above)

The transport question is "how do we speak the protocol"; the certificate
question is "where does the TLS *identity* (cert + key) come from." Same shape,
same zero-dep-first discipline, same single point where a dependency is warranted.
The TLS handshake/record layer itself is always OpenSSL via stdlib `ssl` — this
table is only about minting/sourcing the certificate.

| Tier | Cert source | How | Crypto source | Install | Trust | Status |
|------|-------------|-----|---------------|---------|-------|--------|
| **0a — Core: user-provided** | User's own cert/key (PEM) | `--tls-cert`/`--tls-key` (+ `--tls-password-file`); `--tls-help` prints an `openssl` one-liner for users who want to make one | **stdlib `ssl`** loads it (`load_cert_chain`) | `pip install servery` — **zero deps** | Whatever the user's cert is (can be publicly-trusted) | **Shipped** |
| **0b — Core: ad-hoc self-signed** | Generated at servery startup | `--tls-self-signed`; `_certgen.py` mints RSA-2048 + self-signed X.509 in **pure Python**, writes to a 0600 temp dir, loads via OpenSSL, deletes | **stdlib only** — pure-Python `pow`/`hashlib`/`secrets` + hand-rolled DER + PKCS#1 v1.5; **no `cryptography`, no `openssl` binary, no `ctypes`** | `pip install servery` — **zero deps** | **Untrusted** — opportunistic encryption on a dev box / LAN; clients see an "untrusted certificate" warning; **not a trust anchor** | **Shipped** |
| **1 — Optional extra: ACME / publicly-trusted** | A CA (Let's Encrypt) via the ACME protocol, auto-renewed | (future) `servery[acme]` extra — e.g. `cryptography` + an ACME client; needs a public domain reachable on :80/:443 | PyPI crypto (the one TLS capability that warrants a dep) | `pip install servery[acme]` | **Publicly trusted** | **Not implemented — documented as the boundary** |

**How to read this table.** Tiers 0a and 0b add **no PyPI dependency** — both are
pure stdlib, exactly like Tier 0 of the transport model. Tier 1 (ACME) is the TLS
analogue of the HTTP/3 `servery[http3]` extra: the full ACME protocol + robust
long-lived-key crypto + a public domain on :80/:443 is production-public-web-server
territory (Caddy's lane), at the edge of servery's dev/LAN scope, and is the one
place a TLS dependency is justified. It is **not implemented**; it is recorded here
as the boundary so it is not mistaken for a current feature.

**Validation.** The HTTPS surface (including the `_certgen.py` self-signed cert) is
audited with [`testssl.sh`](https://testssl.sh), the industry-standard TLS scanner —
run `make scan-tls` (or `scripts/scan_tls.sh`). Expected, and confirmed: SSLv2/v3
and TLS 1.0/1.1 **not offered**; TLS 1.2 + 1.3 only; forward-secret **AEAD-only**
ciphers (CBC dropped, so Lucky13/SWEET32 are off the table); SHA-256/RSA-2048 cert
with the requested SAN and hostname trust **OK via SAN**; every CVE check
(Heartbleed, ROBOT, POODLE, FREAK, LOGJAM, BEAST, DROWN, CRIME, …) **not
vulnerable**. The self-signed "chain of trust" is reported incomplete — correct,
because it is self-signed. `tests/test_tls.py::TlsHardeningTest` re-asserts the key
findings (modern protocols + AEAD/forward-secret ciphers + legacy-TLS rejection) as
a stdlib CI regression.

**The `_certgen.py` finding (parallel to the `_oscrypto.py` finding in §4).** §4
established that OS crypto unreachable in pure stdlib (QUIC AEADs) is reachable by
**binding already-present OS libraries via `ctypes`** — the `_oscrypto.py` shim.
Certificate minting is the *opposite* finding on the same spectrum: the stdlib
`ssl` module has no X.509 builder and no asymmetric keygen, but the gap is closable
**without even leaving pure Python** — arbitrary-precision `pow` (modular
exponentiation/inverse), `hashlib` (SHA-256), and `secrets` (CSPRNG) are exactly
enough to generate an RSA key and sign a self-signed certificate (`_certgen.py`),
on a bare Windows/Linux Python with **zero `ctypes` and zero PyPI**. The discipline
that keeps this honest: only **key generation and signing our own cert once at
startup** is hand-rolled; the handshake, key exchange, and record encryption all
stay in OpenSSL via `ssl`. The side-channel concerns that plague hand-rolled crypto
(timing, padding oracles) do not apply to one-shot self-cert generation. So the
sourcing order from §4 holds, with cert-minting slotting in ahead of any `ctypes`
step: **pure stdlib (incl. `_certgen.py` for self-signed) → OS library via `ctypes`
→ vetted PyPI extra (ACME, explicit opt-in only).**

---

## 5. Per-version scope: what "done" means, effort, and risk

### HTTP/1.1 — Tier 0 (now)

**Done = the whole of `STANDARDS.md`.** Conformant HTTP/1.1 origin server (RFC
9110/9111/9112): Range/`206`, full conditional ladder + `ETag`, `Cache-Control`,
`Content-Disposition`, Basic auth, persistent connections, `Host`-required `400`.
Pure stdlib. ALPN advertises `http/1.1`. **Effort: shipped. Risk: low** (it is the
audited stdlib base plus named, tested seams).

### HTTP/2 — Tier 1 (pure stdlib, preferred) / Tier 1′ (`h2`)

**Done means** a correct, *safe* h2 server: binary framing (RFC 9113 §4/§6 — at
least HEADERS, CONTINUATION, DATA, SETTINGS, WINDOW_UPDATE, RST_STREAM, PING,
GOAWAY, PRIORITY-tolerant), a complete **HPACK** encoder/decoder (RFC 7541) with
its dynamic table and Huffman coding, the **stream state machine** (RFC 9113
§5.1), connection- and stream-level **flow control** (§5.2), ALPN `h2` negotiation
with h1.1 fallback, and — non-negotiably — the DoS limits in §6 below. "Done" is
**not** "it passes a happy-path curl"; it is "it survives a hostile peer." A
backend that cannot enforce the §6 limits is **not done**.

- **Tier 1 effort: high** (~2–4k LOC of owned protocol code) — but **zero new
  crypto and zero PyPI deps**. This is the preferred path precisely because it
  keeps the zero-dep promise while adding a real modern transport.
- **Tier 1′ effort: low** — delegate framing/HPACK to `h2`; servery owns sockets,
  ALPN wiring, and dispatch into its handler. Offered for users who want a
  mature, externally maintained implementation; it costs the `servery[http2]`
  extra.
- **Risk: medium-to-high either way** — see §6; the risk is *protocol DoS
  surface*, not crypto.

### HTTP/3 — Tier 2 (`aioquic`, now) / Tier 3 (native ctypes, experimental)

**Done means** HTTP/3 (RFC 9114) over QUIC (RFC 9000) with **QPACK** (RFC 9204),
ALPN `h3`, a UDP listener, `Alt-Svc` advertisement from the TCP tiers, and correct
QUIC packet protection (RFC 9001).

- **Tier 2 (`aioquic`) — the pragmatic supported path, available now.** `aioquic`
  (aiortc; deps `cryptography` + `pylsqpack`; the same stack used by hypercorn,
  mitmproxy, dnspython) provides QUIC + h3 + QPACK; servery binds UDP and drives
  the loop. **Effort: low-to-medium. Risk: low** (mature, widely deployed). Cost:
  the `servery[http3]` PyPI extra.
- **Tier 3 (native `ctypes`→OpenSSL ≥ 3.5) — experimental, zero-PyPI, system-gated.**
  servery's own h3/QPACK glue over OpenSSL's QUIC server + QUIC-TLS via `ctypes`
  (§4). **Effort: high. Risk: high** — you own the FFI *and* the crypto-correctness
  burden (nonce uniqueness, header protection, key update — §4) *and* QPACK *and*
  the OpenSSL-3.5 availability gate. This is a legitimate, advanced backend for
  hosts that have OpenSSL 3.5 and want HTTP/3 with no PyPI footprint — **not** the
  default, and clearly labeled experimental until it has soaked.

---

## 6. HTTP/2 required mitigations (CVEs are non-optional)

HTTP/2's multiplexing is its DoS surface. Any h2 backend — Tier 1 **or** Tier 1′ —
**MUST** enforce the following. These are not hardening niceties; shipping h2
without them ships a known-vulnerable server. (Tier 1′ note: `h2` exposes the
knobs, but servery still MUST set conservative limits — defaults are not safe by
omission.)

| Threat | CVE | Required mitigation |
|--------|-----|---------------------|
| **HTTP/2 Rapid Reset** — flood of `HEADERS` immediately followed by `RST_STREAM`, cheaply churning server work without ever hitting the concurrent-stream cap. | **CVE-2023-44487** | Enforce `SETTINGS_MAX_CONCURRENT_STREAMS`; track a **reset budget** — count rapid open→reset cycles and `GOAWAY`/drop a connection that exceeds it. |
| **CONTINUATION flood** — an unbounded sequence of `CONTINUATION` frames with no terminating `END_HEADERS`, accumulating header state. | **CVE-2024-27316** | **Cap total `CONTINUATION` frames and accumulated header-block size per request**; abort the connection past the cap before buffering more. |
| **HPACK bombs** — small compressed headers that decompress to huge header lists, or dynamic-table abuse. | (HPACK class) | Enforce a **maximum decompressed header-list size** and a bounded dynamic table; a decode that exceeds limits is a `COMPRESSION_ERROR` connection error (RFC 9113 §4.3). |
| **SETTINGS / PING floods** — peer spamming `SETTINGS` or `PING` to force ACK work. | (frame-flood class) | **Rate-limit / budget** inbound `SETTINGS` and `PING`; `GOAWAY` an abusive peer. |

Plus the baseline RFC 9113 limits: `SETTINGS_MAX_CONCURRENT_STREAMS` (bound
concurrency), `SETTINGS_MAX_FRAME_SIZE` (bound per-frame allocation), and
`SETTINGS_MAX_HEADER_LIST_SIZE` (bound header memory). Each MUST have a test that
asserts the server `GOAWAY`s / drops rather than degrades under the corresponding
attack pattern. **This is what "done" for h2 means in §5.**

> These mitigations carry over conceptually to HTTP/3 as well (stream/flow limits
> on QUIC), but h3's distinct surface lives with its backend (`aioquic` already
> implements them; the Tier 3 native backend must, too).

---

## 7. What stays out — and what is merely experimental

Nothing in the HTTP-version space is now **flatly** out. The old "HTTP/2 and
HTTP/3 are permanently out" line is **superseded** by this tiered model. But the
boundaries are explicit:

- **The zero-dependency CORE is never burdened.** HTTP/2 and HTTP/3 are tiers; the
  core remains pure-stdlib HTTP/1.1 with **zero PyPI deps**. No h2/h3 code, no
  optional-library import, and no `ctypes`-crypto module is ever imported on the
  default GET path. `pip install servery` stays empty-`dependencies` forever
  (`ARCHITECTURE.md` §8). This is absolute and outranks everything in this doc.
- **HTTP/3-native (Tier 3) is experimental, not default.** It is gated on OpenSSL
  ≥ 3.5 being present, it carries an owned FFI + crypto-correctness burden, and it
  stays clearly labeled experimental until it has real soak time. Users who want a
  supported h3 today use Tier 2 (`aioquic`).
- **h2c cleartext and h2/h3 "prior knowledge" gymnastics are out.** servery is
  TLS-first for h2/h3; ALPN over TLS (h2) and a UDP listener + `Alt-Svc` (h3) are
  the only negotiation paths. No `Upgrade: h2c`, no plaintext h2.
- **No framework leak.** Backends own transport only; the file-serving policy
  (path safety, listing, range, auth, upload) stays in the single handler
  pipeline. Adding a transport must never become adding a route table, middleware
  chain, or plugin API (`PRINCIPLES.md` §2).
- **Still genuinely out (unchanged):** Markdown rendering, app routes, vendored
  parsers — these fail the scope rubric for reasons that have nothing to do with
  transports (`PRINCIPLES.md` §7).

---

## 8. Summary — the transport posture

servery is a **pure-stdlib HTTP/1.1 file server at its core**, with **HTTP/2 and
HTTP/3 offered as opt-in transport tiers that never burden that core**. HTTP/2 is
*preferred pure-stdlib* (no extra crypto needed — `ssl` gives TLS+ALPN, HPACK and
framing are pure code) with an optional `h2` backend (`servery[http2]`); either
way it **must** ship the CVE-2023-44487 / CVE-2024-27316 / HPACK-bomb /
frame-flood mitigations to count as done. HTTP/3 cannot be pure stdlib, so it is
an optional tier: the supported `aioquic` extra (`servery[http3]`) today, plus an
**experimental, system-gated `ctypes`→OpenSSL ≥ 3.5 native backend** that delivers
HTTP/3 with **zero PyPI dependencies** by binding crypto already on the host. The
crypto-sourcing rule that makes the latter possible — **stdlib first, then
OS-provided crypto via `ctypes` (vetted high-level AEAD only, never hand-rolled),
then PyPI crypto only as an explicit extra** — is the policy of record, with eyes
open about the FFI, OS-availability, and nonce/tag-correctness burdens we
deliberately own.
