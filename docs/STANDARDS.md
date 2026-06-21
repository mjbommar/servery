# servery — Standards / RFC-Compliance Specification

> Companion to `VISION.md`, `PRINCIPLES.md`, `REQUIREMENTS.md`, and
> `ARCHITECTURE.md`. This document is the **RFC-compliance map** for servery as a
> static file server. It says, per HTTP feature, exactly what the relevant RFC
> requires (MUST / SHOULD / MAY, with section citations), what the stdlib
> `http.server` base does today, and what servery must do to be compliant.
>
> **Supreme constraint (outranks everything below): ZERO third-party
> dependencies — pure Python standard library only, forever** (`PRINCIPLES.md`
> §0). Any compliance target that could only be met by adding a dependency is, by
> that rule, out of scope, and is recorded as such with its concrete justification.

All RFC citations are to the **primary source text** (read directly, not from
memory). Citations are written inline as, e.g., "9110 §14.1.2". Requirement levels
(MUST / SHOULD / MAY / MUST NOT) are quoted from the cited RFC.

---

## 1. Normative target

### 1.1 What servery targets

servery is an **HTTP/1.1 origin server for static representations**, conformant to:

| RFC | Title | Role for servery |
|-----|-------|------------------|
| **RFC 9110** | HTTP Semantics | THE core: methods, status codes, Range (§14), conditionals (§13), representation metadata + validators (§8), Date (§6.6.1), Vary (§12.5.5). |
| **RFC 9111** | HTTP Caching | `Cache-Control` directives servery emits, validator/freshness model. |
| **RFC 9112** | HTTP/1.1 | Message framing, persistent connections, `Host`, chunked coding, request-target. |
| **RFC 6266** | Content-Disposition | `attachment`/`inline`, `filename`/`filename*` for downloads and on-the-fly archives. |
| **RFC 8187** | Param value encoding | UTF-8 `ext-value` for `filename*` (supersedes RFC 5987). |
| **RFC 7617** | HTTP Basic auth | `WWW-Authenticate: Basic`, `401`, base64, `charset="UTF-8"`. |
| **RFC 8246** | HTTP Immutable Responses | `Cache-Control: immutable` (NOT in 9111 — cited where used). |

servery declares `protocol_version = "HTTP/1.1"` and emits `HTTP/1.1` in the
status-line (9112 §4, §2.3). This is a **deliberate override of the stdlib
default** (`http.server` sets `protocol_version = "HTTP/1.0"`, line 710), which
turns on persistent connections and enables the framing guarantees below.

### 1.2 What the CORE does not speak — and the optional tiers that do

> **Reframed (see `docs/TRANSPORTS.md`).** HTTP/2 and HTTP/3 are **not part of the
> zero-dependency CORE** — the core is HTTP/1.1, and its TLS ALPN advertises only
> `http/1.1`. They are **no longer flatly out**, however: they are optional, opt-in
> **transport tiers** that never burden the core. HTTP/2 is in fact **feasible in
> pure stdlib** (the preferred tier) — TLS/ALPN is already `ssl`, and HPACK +
> binary framing are pure data + code with *no extra crypto*; the catch is size
> (~2–4k LOC) and a real DoS-mitigation burden, not a dependency. HTTP/3 genuinely
> cannot be pure stdlib (QUIC needs AEAD ciphers the stdlib lacks) and is offered
> via the `aioquic` extra or an experimental `ctypes`→OpenSSL ≥ 3.5 native backend.
> The per-tier mechanism, crypto-sourcing policy, install matrix, and CVE
> mitigations are the subject of `docs/TRANSPORTS.md`. The technical facts below
> remain accurate and explain *why the core stays HTTP/1.1*.

**HTTP/2 (RFC 9113) — not in the core (optional Tier 1; pure-stdlib-feasible).**
Why it is not core, and what an h2 tier must build:

- **HPACK is mandatory and absent from the stdlib.** 9113 §4.3 / §4.3.1 require
  stateful HPACK (RFC 7541) encoder/decoder contexts; a decode failure MUST be a
  `COMPRESSION_ERROR` connection error. The Python standard library ships **no
  `hpack` module** — so the h2 tier must **hand-roll HPACK** (feasible: it is pure
  data + code, incl. the static Huffman table, *no crypto*) **or** delegate to the
  `h2`/`hpack` libraries via the `servery[http2]` extra. Either way it is not
  reachable by the core as-is.
- **Binary framing layer.** 9113 §4 / §4.1 / §6 replace the text protocol
  `http.server` parses with a binary framing layer (~10 frame types: HEADERS,
  DATA, SETTINGS, WINDOW_UPDATE, RST_STREAM, GOAWAY, CONTINUATION, …). None exists
  in the stdlib.
- **Stream multiplexing, state machine, flow control.** 9113 §5.1 (stream
  states) and §5.2 (connection- and stream-level flow control via WINDOW_UPDATE)
  are substantial machinery; `http.server` is one-request-per-connection with no
  stream concept.
- **Connection preface + ALPN "h2".** 9113 §3.4 requires the 24-octet client
  preface; §3.2 defines ALPN id `h2`. Strict TLS rules (9113 §9.2 / §9.2.2 +
  Appendix A: TLS 1.2+, cipher blocklist, SNI) add further machinery.

**HTTP/3 (RFC 9114) — not in the core (optional Tier 2/3; not pure-stdlib).**
Genuinely impossible in pure stdlib, which is why it is a dependency-bearing or
system-gated tier rather than core:

- **Runs over QUIC.** 9114 §1.2 / §6 map HTTP semantics onto QUIC (RFC 9000); the
  stdlib has **no QUIC transport**.
- **QPACK, not HPACK.** 9114 §2 replaces HPACK with QPACK (RFC 9204), also absent
  from the stdlib. ALPN id is `h3`.
- **Needs AEAD crypto the stdlib does not have.** QUIC packet protection (RFC
  9001) requires AES-128-GCM / ChaCha20-Poly1305 + HKDF, and a TLS-1.3 handshake
  driven as bytes inside QUIC frames — the stdlib ships **no symmetric ciphers**
  and `ssl` cannot drive a QUIC handshake. The supported tier uses the `aioquic`
  extra (`servery[http3]`); an **experimental** zero-PyPI backend binds system
  OpenSSL **≥ 3.5** (QUIC server + QUIC-TLS) via `ctypes` (`docs/TRANSPORTS.md`).

**ALPN consequence (normative for servery's TLS path):** the **core** does not
speak `h2` (9113 §3.2) or `h3` (9114), so the core's TLS `SSLContext` MUST
advertise **only `http/1.1`** via ALPN — which is exactly what
`http.server.HTTPSServer` already does (`set_alpn_protocols(["http/1.1"])`).
servery MUST NOT advertise (via ALPN or `Alt-Svc`) a protocol it cannot speak in
the current build: when the optional HTTP/2 tier is enabled it advertises `h2`
(falling back to `http/1.1`), and when an HTTP/3 tier is enabled it advertises
`h3` via `Alt-Svc` — otherwise neither is advertised. → **NFR-STD-01**.

> Recorded so the framing is not re-litigated: HTTP/2 and HTTP/3 are **not in the
> zero-dependency core** (the core is HTTP/1.1), but they are **optional opt-in
> transport tiers**, not flatly excluded — see `docs/TRANSPORTS.md`. The core never
> imports them.

---

## 2. Per-area compliance checklists

Legend: **[M]** MUST, **[S]** SHOULD, **[A]** MAY, **[MN]** MUST NOT, **[SN]**
SHOULD NOT. "Base?" = does stdlib `http.server` already satisfy it.

### 2.1 Range / partial content (9110 §14)

Range is an **OPTIONAL** HTTP feature (9110 §14: a server "MAY ignore the Range
header field"). servery elects to implement it (`DEC-RANGE`). Once implemented,
the following hold:

| # | Requirement | Lvl | Cite | Base? |
|---|-------------|-----|------|-------|
| R1 | Advertise byte-range support on successful file responses: `Accept-Ranges: bytes`. | S | 9110 §14.3 | No |
| R2 | Honor a single satisfiable `Range: bytes=a-b` → `206 (Partial Content)`. | S | 9110 §14.2 ("SHOULD send a 206") | No |
| R3 | `206` MUST carry `Content-Range: bytes a-b/total` (single-part). | M | 9110 §14.4 | No |
| R4 | Open-ended `bytes=a-`: if last-pos absent or ≥ length, serve remainder (server replaces last-pos with `length-1`). | — | 9110 §14.1.2 | No |
| R5 | Suffix `bytes=-N`: last N bytes; if representation shorter than N, serve the whole representation. | — | 9110 §14.1.2 | No |
| R6 | Unsatisfiable range (e.g. first-pos ≥ length) → `416 (Range Not Satisfiable)`. | S | 9110 §14.2 | No |
| R7 | `416` SHOULD carry `Content-Range: bytes */complete-length` (the unsatisfied-range form; complete-length = current size). | S | 9110 §14.4 | No |
| R8 | Ignore `Range` on any method other than GET (range handling is defined only for GET). | M | 9110 §14.2 | n/a |
| R9 | Anticipate large decimal numerals; prevent integer-overflow parse errors. | M | 9110 §14.1.2, §14.2 | No |
| R10 | A range with last-pos < first-pos is **invalid**; server MAY ignore/reject an invalid ranges-specifier and respond as a normal GET (`200`). | A | 9110 §14.1.1, §14.2 | No |
| R11 | Evaluate `Range` **after** precondition fields (§13.1) and **only if** the result without `Range` would be `200` (i.e. ignore `Range` when the conditional GET yields `304`). | M | 9110 §14.2 | No |
| R12 | Honor `If-Range` (§13.1.5) as a precondition to applying `Range` (see §2.2 R-precedence). | A | 9110 §14.2, §13.1.5 | No |
| R13 | `Range` honored identically on `HEAD` (`206`/`416` status + headers, empty body). | — | 9110 §9.3.2 (HEAD = GET headers) | No |
| R14 | **Multiple ranges / `multipart/byteranges`:** servery **MAY** support it; v1 chooses **NOT** to. A multi-range request is treated as not-supported and served `200` full body (allowed: §14.2 "A server that supports range requests MAY ignore ... a ranges-specifier with more than two overlapping ranges"). If ever added, each part MUST carry its own `Content-Type` + `Content-Range` and `Content-Type: multipart/byteranges; boundary=…` (§14.6). | A | 9110 §14.2, §14.6 | No |

**servery action:** implement `ranges.py` (single-range parser → `(start,end)`;
`206`/`Content-Range`/`Accept-Ranges`; `416 + Content-Range: bytes */len`;
malformed/multi-range → fall back to `200`). Maps to **FR-RANGE-01..06**.

**Edge note (zero-length file, R6):** for a GET, the only satisfiable spec on a
zero-length representation is a suffix-range with non-zero suffix-length (9110
§14.1.2). servery may simply treat any range on an empty file as unsatisfiable →
`416`, or ignore `Range` and serve the empty `200` (9110 §14.2 permits ignoring
`Range` when the representation has zero length).

### 2.2 Conditional requests (9110 §13)

| # | Requirement | Lvl | Cite | Base? |
|---|-------------|-----|------|-------|
| C1 | `If-Modified-Since` (GET/HEAD only) → `304` when not modified; ignore if invalid date / multiple members / non-GET-HEAD / no mod-date. | M (ignore rules), S (304) | 9110 §13.1.3 | **Yes** (`send_head`, ~line 827) |
| C2 | `ETag` SHOULD be sent for any representation where change detection can be reasonably and consistently determined. | S | 9110 §8.8.3.1 | No |
| C3 | `If-None-Match`: use **weak comparison**; GET/HEAD false → `304`; other methods false → `412`. `*` matches any current representation. | M | 9110 §13.1.2 | No |
| C4 | `If-Modified-Since` MUST be **ignored** if `If-None-Match` is present. | M | 9110 §13.1.3 | **Partial** — base honors `If-Modified-Since` but is unaware of `If-None-Match`. |
| C5 | `If-Match`: use **strong comparison**; false → `412`. `*` = any current representation. | M (compare), A (412) | 9110 §13.1.1 | No |
| C6 | `If-Unmodified-Since`: false → `412`; MUST be ignored if `If-Match` present. | M (ignore), A (412) | 9110 §13.1.4 | No |
| C7 | `If-Range` (§13.1.5): match → process `Range` (`206`); no match → ignore `Range`, serve whole `200`. Date form valid only if it is a **strong** validator and **exactly** equals `Last-Modified`; entity-tag form uses **strong comparison**; a **weak** ETag MUST NOT be used by the client in `If-Range`. | M | 9110 §13.1.5 | No |
| C8 | **Precedence / evaluation order** (§13.2.2): 1.`If-Match` → 2.`If-Unmodified-Since` → 3.`If-None-Match` → 4.`If-Modified-Since` → 5.`If-Range`. | M | 9110 §13.2.2 | No |
| C9 | Ignore **all** preconditions if the unconditioned response would be other than `2xx` or `412`. | M | 9110 §13.2.1 | No |
| C10 | A `304` MUST NOT contain a body (terminated by end of header section). | M | 9110 §15.4.5 | **Yes** |
| C11 | A `304` MUST echo the validator/cache fields it would have sent on `200`: `ETag`, `Date`, `Vary`, `Cache-Control`, `Content-Location`, `Expires`; SHOULD include `Last-Modified` if there is no `ETag`. | M / S | 9110 §15.4.5 | **Partial** (base sends `Date`; no `ETag`). |
| C12 | If `Content-Length` is sent on a `304`, it MUST equal the `200` body length. | M | 9110 §8.6 | n/a |

**Precedence wiring (C8), restated for `send_head`:**

1. `If-Match` present & false → `412`.
2. else `If-Unmodified-Since` present & false → `412`.
3. else `If-None-Match` present & false → `304` (GET/HEAD) / `412` (other).
4. else (`If-None-Match` absent) `If-Modified-Since` present & false → `304`.
5. else (GET, both `Range` + `If-Range`) `If-Range` true → apply `Range`; false →
   ignore `Range`, serve `200`.
6. else perform the method normally.

**Recommended servery ETag construction.** Build the validator from filesystem
metadata available cheaply via `os.stat`: **size + mtime (+ optionally inode)**,
e.g. `'W/"%x-%x"' % (st.st_size, st.st_mtime_ns)`. This MUST be marked **weak**
(`W/` prefix, case-sensitive — 9110 §8.8.3): an mtime/size validator does not
satisfy the strong-validator characteristics (9110 §8.8.1 — "a modification time
defined with only one-second resolution ... might be a weak validator if it is
possible for the representation to be modified twice during a single second"). A
**strong** ETag would require hashing the content (e.g. `hashlib.sha256` of the
file), which servery MAY offer behind a flag for callers who need strong
validation (e.g. usable in `If-Range` with byte ranges), but does NOT do by
default for performance. The ETag MUST be quoted (`DQUOTE opaque-tag DQUOTE`,
9110 §8.8.3) and MUST NOT collide across distinct representations (size+mtime_ns
is a sound weak validator; mtime_ns avoids the one-second collision window).

**Consequence for `If-Range` (C7):** because servery's default ETag is **weak**,
clients MUST NOT use it in `If-Range` (9110 §13.1.5), and a `Last-Modified`-based
`If-Range` only counts as strong when the client's stored `Date` is at least one
second after `Last-Modified` (9110 §8.8.2.2). servery evaluates `If-Range`
conservatively: date form requires an **exact** `Last-Modified` match; a weak
ETag in `If-Range`, if a client sends one, is treated as **no match** → serve
full `200`. → **FR-CACHE-02** (extend), new **FR-COND-01** (precedence),
**FR-COND-02** (`If-Range`).

### 2.3 Caching (9111, + RFC 8246)

| # | Requirement | Lvl | Cite | Base? |
|---|-------------|-----|------|-------|
| K1 | A static origin SHOULD emit a validator (`ETag` and/or `Last-Modified`) so caches can revalidate cheaply. | S | 9111 §3, §4.3.1; 9110 §8.8 | **Partial** (`Last-Modified` only). |
| K2 | `Cache-Control: max-age=N` — response stale after age > N seconds. | — | 9111 §5.2.2.1 | No |
| K3 | `Cache-Control: no-cache` — MAY be stored, but MUST be revalidated before reuse. | — | 9111 §5.2.2.4 | No |
| K4 | `Cache-Control: no-store` — MUST NOT be stored at all. | — | 9111 §5.2.2.5 | No |
| K5 | `Cache-Control: immutable` — fresh response SHOULD NOT be revalidated on reload for its freshness lifetime. **Defined in RFC 8246 §2, NOT in 9111.** | S | RFC 8246 §2 | No |
| K6 | `Cache-Control: public` / `private` — shareability marker. | — | 9111 §5.2.2.9 / §5.2.2.7 | No |
| K7 | Heuristic freshness: absent explicit expiry, a cache MAY use a fraction (~10%) of `(Date − Last-Modified)`. servery emitting explicit `max-age` or a validator pre-empts surprising heuristic caching. | A (cache side) | 9111 §4.2.2 | n/a |

**servery default posture (`DEC-CACHE`).** Default is a **conservative
`no-cache`** for a dev tool: `Cache-Control: no-cache` so clients always
revalidate (cheap with `Last-Modified`/`ETag`), avoiding stale-asset confusion
during development. `-c <seconds>` opts into `Cache-Control: max-age=<n>`;
`-c -1` / `--no-cache` emits `Cache-Control: no-cache, no-store`. An `immutable`
posture (e.g. `--cache <n> --immutable` for content-hashed assets) MAY be offered,
citing RFC 8246. servery always emits `Last-Modified` and (per K1) SHOULD emit
`ETag`, so that even under `no-cache` revalidation is a cheap `304`. Maps to
**FR-CACHE-01**, **FR-CACHE-02**.

### 2.4 Representation metadata (9110 §8) + MIME-sniffing defense

| # | Requirement | Lvl | Cite | Base? |
|---|-------------|-----|------|-------|
| M1 | Send `Content-Type` for responses with content; if media type unknown, omit or use `application/octet-stream`. | S | 9110 §8.3, §8.3.1 | **Yes** |
| M2 | Textual types SHOULD carry a `charset` parameter (e.g. `text/html; charset=utf-8`); charset names matched case-insensitively. | — | 9110 §8.3.2 | **Partial** (base sets charset for some). |
| M3 | `Content-Length` = decimal octet count of the representation; origin SHOULD send it when size is known before headers (absent `Transfer-Encoding`). | S | 9110 §8.6 | **Yes** |
| M4 | On `HEAD`, if `Content-Length` is sent it MUST equal the GET body length. | M | 9110 §8.6 | **Yes** |
| M5 | `Last-Modified = HTTP-date`; origin SHOULD send it; MUST NOT send a value later than the message origination time (`Date`). | S / MN | 9110 §8.8.2, §8.8.2.1 | **Yes** (sends `Last-Modified`). |
| M6 | `Date` header in **IMF-fixdate** format (`Sun, 06 Nov 1994 08:49:37 GMT`); origin with a clock MUST send `Date` on all 2xx/3xx/4xx. | M | 9110 §6.6.1, §5.6.7 | **Yes** (`date_time_string()` emits IMF-fixdate). |
| M7 | `Vary` lists request fields that influenced selection; send it when content is negotiated (e.g. `Vary: Accept-Encoding` if compression is ever negotiated). | — | 9110 §12.5.5 | No |
| M8 | **`X-Content-Type-Options: nosniff`** — disables client MIME-sniffing. **Not an IETF RFC** (WHATWG Fetch / web standard); 9110 §8.3 explicitly warns MIME sniffing "risks ... additional security risks (e.g., 'privilege escalation')". **Security-relevant.** | (web std) | 9110 §8.3 (rationale); WHATWG Fetch | No |

**servery action.** Continue to route MIME detection through
`mimetypes.guess_file_type` (the 3.13 path-aware API, per `FR-SERVE-02`). Emit a
correct IMF-fixdate `Date` (inherited). **Emit `X-Content-Type-Options: nosniff`
by default** — for a file server that serves arbitrary user content, sniffing is
the classic stored-XSS vector (a `.txt` sniffed as `text/html`). This is a
security default, gated only by a `-H` override if a user truly needs sniffing.
→ new **FR-SEC-04** (nosniff default). When/if gzip negotiation is added,
responses MUST carry `Vary: Accept-Encoding` (M7). → **FR-SERVE-02**, **FR-HDR-01**.

### 2.5 HTTP/1.1 framing & connections (9112)

| # | Requirement | Lvl | Cite | Base? |
|---|-------------|-----|------|-------|
| H1 | Status-line emits `HTTP/1.1`; MUST send the SP separating status-code from (possibly empty) reason-phrase. | M | 9112 §4, §2.3 | **Base default is HTTP/1.0** — servery overrides `protocol_version`. |
| H2 | Persistent connections are the HTTP/1.1 **default**; implementations SHOULD support them. | S | 9112 §9.3 | Only if `protocol_version="HTTP/1.1"` (off by default in base). |
| H3 | A server NOT supporting persistence MUST send `Connection: close` in every non-1xx response. If servery keeps connections alive, it MUST honor a client `Connection: close`. | M | 9112 §9.3, §9.6 | **Yes** (base `close_connection` logic, once HTTP/1.1). |
| H4 | A server receiving the `close` connection option MUST close after the final response and MUST NOT process further requests on it. | M / MN | 9112 §9.6 | **Yes** |
| H5 | A server MUST read the entire request body (or close the connection) before reuse — relevant to upload (`do_POST`). | M | 9112 §9.3 | servery must ensure upload drains/closes. |
| H6 | `Host`: MUST respond `400 (Bad Request)` to any HTTP/1.1 request lacking `Host`, with >1 `Host` field line, or an invalid `Host` value. | M | 9112 §3.2 | **Partial** — base validates request line but does not enforce the Host-presence `400`. |
| H7 | Request-target forms: accept **origin-form** (the normal case); MUST accept **absolute-form**; authority-form only for CONNECT; asterisk-form only for server-wide OPTIONS. | M | 9112 §3.2.1–§3.2.4 | **Partial** (base handles origin-form). |
| H8 | Body length framing: MUST NOT send `Content-Length` together with `Transfer-Encoding`; `Transfer-Encoding` overrides `Content-Length`. | MN | 9112 §6.1, §6.2, §6.3 | **Yes** (base never combines them). |
| H9 | Chunked is the final transfer-coding; usable when content length is unknown in advance — for streaming archives (`tar.gz`) and unknown-length responses. Format: `chunk-size`(hex) CRLF data CRLF … `0` CRLF [trailers] CRLF. | M (final coding) | 9112 §6.1, §7.1 | servery must emit valid chunked for streaming archives. |
| H10 | Over-long request-target MUST → `414 (URI Too Long)`; malformed message SHOULD → `400` + close; whitespace between field-name and colon MUST → `400`; RECOMMENDED to support request-line ≥ 8000 octets. | M / S / RECOMMENDED | 9112 §3, §5.1, §2.2 | **Yes** (base `http.client`/`BaseHTTPRequestHandler` field parsing). |
| H11 | obs-fold (line folding) is deprecated: a server receiving obs-fold outside `message/http` MUST reject (`400`) or replace each fold with SP. | M | 9112 §5.2 | **Yes** (base parser). |

**servery action.** Set `protocol_version = "HTTP/1.1"` (`server.py`/handler) to
enable keep-alive (H1/H2) and the framing guarantees; rely on the base for H3, H4,
H8, H10, H11. **Add an explicit `Host`-presence check (H6)** returning `400` for a
missing/duplicate `Host` on HTTP/1.1 — the base class does not enforce this, and a
modern HTTP/1.1 server MUST. For streaming archives (`FR-ARCHIVE-02`), emit valid
**chunked** transfer-coding (H9) — `tarfile.open(fileobj=wfile, mode="w|gz")`
streams with no `Content-Length`, so the response MUST be framed by chunked
transfer-coding **or** `Connection: close`. → **FR-SERVE-01**, **FR-ARCHIVE-02**,
new **FR-CONN-01** (HTTP/1.1 + keep-alive), new **FR-HOST-01** (Host `400`).

> **Important keep-alive corollary:** once `protocol_version="HTTP/1.1"`, every
> response that streams without a `Content-Length` (chunked archives, zip) MUST
> use either valid chunked framing or `Connection: close`, or the client will hang
> waiting for body bytes that the framing never delimits. This is a required test
> (§4 E9).

### 2.6 Content-Disposition (6266 + 8187)

Used for **explicit downloads** (`?download=` archives, force-download links) and
**on-the-fly archives**.

| # | Requirement | Lvl | Cite | Base? |
|---|-------------|-----|------|-------|
| D1 | Grammar: `Content-Disposition: <disposition-type> *(";" parm)`; type ∈ {`inline`, `attachment`, token}, case-insensitive. | — | 6266 §4.1 | No |
| D2 | Use `attachment` to prompt a save; `inline` for default in-browser handling. Unknown types are handled as `attachment`. | S | 6266 §4.2 | No |
| D3 | Plain `filename` is quoted-string/token, **ISO-8859-1 only** — safe ASCII fallback. | — | 6266 §4.1, §4.3 | No |
| D4 | `filename*` uses the RFC 8187 `ext-value` to carry non-ISO-8859-1 (UTF-8) names. | — | 6266 §4.3 | No |
| D5 | When **both** `filename` and `filename*` are present, recipients SHOULD pick `filename*`. servery emits both: an ASCII-sanitized `filename=` fallback **and** `filename*=UTF-8''<pct-encoded>`. | S | 6266 §4.3 | No |
| D6 | Treat any client-supplied filename as advisory; strip path components (`/` and `\`), control chars, and reserved names (`.`/`..`/`~`/device names) — already covered by upload sanitization (`FR-UPLOAD-04`). | M / S | 6266 §4.3, §5 | No |
| D7 | `ext-value` grammar: `charset "'" [language] "'" value-chars`; producers MUST use `UTF-8`; only `attr-char` left unescaped, all else `%HH` percent-encoded. | M | 8187 §3.2.1 | No |

**Concrete form servery emits** (per 6266 §5 / 8187 §4.2 examples):

```
Content-Disposition: attachment; filename="EURO rates"; filename*=UTF-8''%e2%82%ac%20rates
```

(`%e2%82%ac` = U+20AC `€`, `%20` = space.) For an archive download of directory
`sub`, the header is `attachment; filename="sub.tar.gz"; filename*=UTF-8''sub.tar.gz`
(ASCII names need no `filename*`, but emitting it is harmless and consistent).

**servery action.** Build the header in `httputil.py`/`archive.py`: ASCII-fold the
basename for `filename=`, percent-encode the UTF-8 octets of the original name for
`filename*` (only `attr-char` survive unescaped, per 8187 §3.2.1 — note `space`,
`(`, `)`, `,` are all `%HH`). Maps to **FR-ARCHIVE-01**; new **FR-DISP-01**.

### 2.7 Basic auth (RFC 7617)

| # | Requirement | Lvl | Cite | Base? |
|---|-------------|-----|------|-------|
| B1 | Challenge with `401 (Unauthorized)` + `WWW-Authenticate: Basic realm="…"`. `realm` is **REQUIRED**. | REQUIRED | 7617 §2 | No |
| B2 | MAY add `charset="UTF-8"` (the only allowed value, matched case-insensitively) advising NFC + UTF-8 octets. | A | 7617 §2.1 | No |
| B3 | Credentials = base64(`user-id ":" password`) of the (UTF-8) octets; `Authorization: Basic <b64>`. user-id MUST NOT contain a colon; neither field may contain control characters. | M | 7617 §2 | No |
| B4 | Basic transmits cleartext-equivalent (reversible base64) credentials; SHOULD NOT be used without HTTPS to protect sensitive data → loud no-TLS warning. | SN | 7617 §4 | No |
| B5 | Protection space: authentication scope is the path up to the last `/`; a client MAY preemptively send credentials within that scope. | A | 7617 §2.2 | n/a |

**Exact challenge servery emits:**

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Basic realm="servery", charset="UTF-8"
```

**servery action.** `auth.py` parses `Authorization: Basic <b64>`
(`base64.b64decode`, then UTF-8/`latin-1` decode), splits on the **first** colon
(B3), compares with `hmac.compare_digest` — never `==` (`FR-AUTH-03`,
`NFR-SEC-02`). On no/invalid credentials → `401` + the challenge above (B1/B2).
Emit the no-TLS warning (B4) per `FR-AUTH-04`. Maps to **FR-AUTH-01..05**; the
`charset="UTF-8"` and exact realm form refine **FR-AUTH-01**.

---

## 3. Consolidated compliance-gap table (prioritized)

Priority: **P0** = correctness/standards gap a modern HTTP/1.1 server must close;
**P1** = SHOULD-level / strong recommendation; **P2** = MAY / nice-to-have.

| Pri | Requirement (cite) | Stdlib base? | servery action | FR map |
|-----|--------------------|--------------|----------------|--------|
| P0 | **HTTP/1.1 + persistent connections** (9112 §2.3, §9.3) — base defaults to HTTP/1.0, keep-alive OFF. | **No** | Set `protocol_version="HTTP/1.1"`; honor `Connection: close`; frame streamed bodies with chunked or `Connection: close`. | **FR-CONN-01** (new) |
| P0 | **Range / `206` / `Content-Range`** (9110 §14.2–§14.4) — no Range support at all. | **No** | `ranges.py`: single/suffix/open-ended → `206`; unsatisfiable → `416 + Content-Range: bytes */len`; malformed/multi → `200`. | FR-RANGE-01..06 |
| P0 | **`416` + `Content-Range: bytes */len`** (9110 §14.4) | **No** | Emit on unsatisfiable range. | FR-RANGE-04 |
| P0 | **`Host` presence → `400`** (9112 §3.2) — missing/duplicate `Host` on HTTP/1.1. | **Partial** | Reject with `400` in handler. | **FR-HOST-01** (new) |
| P0 | **Conditional precedence + `If-None-Match`/`If-Match`/`412`** (9110 §13.1, §13.2.2) — base only does `If-Modified-Since`. | **No** | Implement full precedence ladder; `412`; ignore `If-Modified-Since` when `If-None-Match` present. | **FR-COND-01** (new) |
| P0 | **`304` echoes validators, no body** (9110 §15.4.5) | **Partial** | On `304` send `ETag`/`Date`/`Vary`/`Cache-Control`; no body (base already omits body). | FR-CACHE-02, **FR-COND-01** |
| P1 | **`ETag` (weak, size+mtime_ns)** (9110 §8.8.3, §8.8.3.1) | **No** | Emit weak `ETag`; honor `If-None-Match` (weak compare). | FR-CACHE-02 |
| P1 | **`Cache-Control`** (9111 §5.2.2; 8246 §2 for `immutable`) | **No** | `no-cache` default; `max-age=N`; `no-store`; optional `immutable`. | FR-CACHE-01 |
| P1 | **`X-Content-Type-Options: nosniff`** (9110 §8.3 rationale; WHATWG) | **No** | Emit by default (anti-stored-XSS). | **FR-SEC-04** (new) |
| P1 | **`Accept-Ranges: bytes`** on `200` (9110 §14.3) | **No** | Advertise on file responses. | FR-RANGE-01 |
| P1 | **`Content-Disposition` `attachment` + `filename*`** (6266 §4.2/§4.3, 8187 §3.2.1) | **No** | `attachment; filename="…"; filename*=UTF-8''…` for downloads/archives. | **FR-DISP-01** (new), FR-ARCHIVE-01 |
| P1 | **Basic auth: `realm` + `charset="UTF-8"`, constant-time** (7617 §2, §2.1, §4) | **No** | Exact `WWW-Authenticate`; `hmac.compare_digest`; no-TLS warning. | FR-AUTH-01..05 |
| P1 | **Chunked framing for streamed archives** (9112 §7.1) | **Partial** | Valid chunked or `Connection: close` for `Content-Length`-less streaming. | FR-ARCHIVE-02 |
| P2 | **`If-Range`** (9110 §13.1.5) | **No** | Date-exact / strong-ETag gating of `Range`. | **FR-COND-02** (new) |
| P2 | **`If-Unmodified-Since` / `If-Match` for writes** (9110 §13.1.1/§13.1.4) | **No** | Optional optimistic-concurrency guard on upload (`412`). | **FR-COND-03** (new, optional) |
| P2 | **`Vary: Accept-Encoding`** when compression negotiated (9110 §12.5.5) | **No** | Emit iff gzip negotiation ever added (currently out of v1). | FR-SERVE-02 |
| P2 | **`multipart/byteranges`** for multi-range (9110 §14.6) | **No** | Out of v1 (MAY); multi-range → `200`. | FR-RANGE-05 |
| — | **HTTP/2 / HTTP/3** (9113 / 9114) | **No** | **Not in core** (core is HTTP/1.1; ALPN advertises only `http/1.1`). Optional opt-in tiers: h2 pure-stdlib-feasible / `servery[http2]`; h3 via `aioquic` (`servery[http3]`) or experimental `ctypes`→OpenSSL ≥ 3.5. See `docs/TRANSPORTS.md`. | **NFR-STD-01** (new) |
| ✓ | `Date` IMF-fixdate (9110 §6.6.1) | **Yes** | Keep inherited `date_time_string()`. | — |
| ✓ | `Last-Modified` / `If-Modified-Since` → `304` (9110 §8.8.2, §13.1.3) | **Yes** | Keep, extend with precedence (C4). | FR-SERVE-05 |
| ✓ | `Content-Type` / `Content-Length` (9110 §8.3, §8.6) | **Yes** | Keep; route MIME via `guess_file_type`. | FR-SERVE-02 |
| ✓ | Path-traversal / `//` open-redirect (gh-87389) | **Yes** | Reuse `translate_path`; wrap with `realpath` containment. | FR-SERVE-06 |

---

## 4. Edge cases & pitfalls to encode as tests

Each maps to a `unittest` case (stdlib `http.client`, real server — see
`ARCHITECTURE.md` §7).

- **E1 — Malformed `Range` → `200`.** `Range: bytes=abc`, `Range: bytes=` →
  ignore, full `200` body (9110 §14.1.1, §14.2 R10).
- **E2 — last-pos < first-pos is invalid.** `bytes=100-50` → invalid
  ranges-specifier → serve `200` (9110 §14.1.1).
- **E3 — Overlapping / multi-range.** `bytes=0-9,20-29` → v1 serves `200` full
  body (allowed, 9110 §14.2); assert NOT a malformed `206`.
- **E4 — Suffix larger than file.** 1000-byte file, `bytes=-5000` → entire
  representation (9110 §14.1.2); assert `206 Content-Range: bytes 0-999/1000` (or
  `200`, per chosen policy) — pick one and test it.
- **E5 — Open-ended past EOF.** `bytes=999-` on 1000-byte file → last byte;
  `bytes=1000-` → **unsatisfiable** → `416 Content-Range: bytes */1000` (9110
  §14.1.2, §14.4).
- **E6 — Zero-length file + range.** `bytes=0-0` on empty file → `416` (only a
  non-zero suffix-range is satisfiable; 9110 §14.1.2).
- **E7 — `If-Range` with weak validator.** Client sends `If-Range: W/"abc"` +
  `Range:` → servery MUST NOT honor the weak tag as a strong match → serve full
  `200` (9110 §13.1.5). Date-form `If-Range` that exactly matches `Last-Modified`
  → `206`; non-matching date → full `200`.
- **E8 — `HEAD` matches `GET` headers.** `HEAD` returns the **same** status and
  headers as `GET` (incl. `Content-Length`, `Accept-Ranges`, `ETag`,
  `Content-Range` on a ranged HEAD) with an **empty body** (9110 §9.3.2, §8.6 M4;
  FR-RANGE-06).
- **E9 — `304` has no body and echoes validators.** Conditional GET → `304` with
  `ETag` + `Date` (+ `Cache-Control`/`Vary` if sent on `200`) and **zero** body
  bytes; assert `Content-Length: 0` or absent, and that a kept-alive connection is
  not left waiting (9110 §15.4.5; H9 keep-alive corollary).
- **E10 — `If-None-Match` beats `If-Modified-Since`.** Both present →
  `If-Modified-Since` ignored, decision driven by `If-None-Match` (weak compare)
  (9110 §13.1.3 C4). `If-None-Match: *` on an existing file → `304`.
- **E11 — `If-Match` / `If-Unmodified-Since` failure → `412`** (on a write/upload
  path if implemented) (9110 §13.1.1, §13.1.4).
- **E12 — Precedence ladder order.** Construct a request exercising the §13.2.2
  order (e.g. failing `If-Match` + present `If-None-Match`) → `412` wins before
  `If-None-Match` is even evaluated (9110 §13.2.2).
- **E13 — Range ignored when conditional yields `304`.** Satisfiable `Range` +
  `If-None-Match` that matches → `304`, NOT `206` (9110 §14.2 R11).
- **E14 — Obsolete date formats parse.** `If-Modified-Since` in RFC 850
  (`Sunday, 06-Nov-94 08:49:37 GMT`) and asctime (`Sun Nov  6 08:49:37 1994`)
  forms MUST be accepted on input, even though servery only **emits** IMF-fixdate
  (9110 §5.6.7 — recipients MUST accept all three).
- **E15 — Missing / duplicate `Host` → `400`.** HTTP/1.1 request with no `Host`,
  or two `Host` lines → `400` (9112 §3.2; H6/FR-HOST-01).
- **E16 — Over-long request-target → `414`.** A request-target beyond servery's
  parse limit → `414` (9112 §3; H10).
- **E17 — Whitespace before colon → `400`.** `Foo : bar` header → `400` (9112
  §5.1; H10/H11) — typically enforced by the base parser; assert it.
- **E18 — `nosniff` present.** Every file response carries
  `X-Content-Type-Options: nosniff` by default (M8/FR-SEC-04); a `.txt` cannot be
  sniffed as HTML.
- **E19 — `Content-Disposition` UTF-8 filename.** Archive of a directory whose
  name contains `€` yields `filename*=UTF-8''%e2%82%ac…` plus an ASCII
  `filename=` fallback (6266 §4.3, 8187 §3.2.1; D5/D7).
- **E20 — `Vary` on negotiated content.** If gzip negotiation is enabled, a
  negotiated response carries `Vary: Accept-Encoding` so caches key correctly
  (9110 §12.5.5; M7) — currently out of v1, kept as a forward-looking test stub.
- **E21 — Streamed archive framing.** Under `protocol_version="HTTP/1.1"`, a
  `tar.gz` download (no `Content-Length`) is delimited by valid chunked
  transfer-coding or `Connection: close`; a keep-alive client receives a complete,
  non-hanging response (9112 §7.1; H9 corollary).
- **E22 — Basic-auth exact challenge.** No-auth request → `401` with
  `WWW-Authenticate: Basic realm="servery", charset="UTF-8"`; a user-id-with-colon
  is rejected; comparison uses `hmac.compare_digest` (7617 §2, §2.1; B1–B3).

---

## 5. Newly proposed requirement IDs (to fold into `REQUIREMENTS.md`)

| ID | Title | Cite | Priority |
|----|-------|------|----------|
| **FR-CONN-01** | Serve as HTTP/1.1 with persistent connections (`protocol_version="HTTP/1.1"`; honor `Connection: close`). | 9112 §2.3, §9.3, §9.6 | P0 |
| **FR-HOST-01** | Reject HTTP/1.1 requests with missing/duplicate/invalid `Host` → `400`. | 9112 §3.2 | P0 |
| **FR-COND-01** | Full conditional-request precedence (`If-Match`/`If-Unmodified-Since`/`If-None-Match`/`If-Modified-Since`) with `304`/`412` and validator-echo on `304`. | 9110 §13.1, §13.2.2, §15.4.5 | P0 |
| **FR-COND-02** | `If-Range` gating of `Range` (date-exact / strong-ETag). | 9110 §13.1.5 | P2 |
| **FR-COND-03** | Optional optimistic-concurrency guard on upload via `If-Match`/`If-Unmodified-Since` → `412`. | 9110 §13.1.1/§13.1.4 | P2 (optional) |
| **FR-DISP-01** | `Content-Disposition: attachment` with `filename=` + `filename*=UTF-8''…` for downloads/archives. | 6266 §4.2/§4.3, 8187 §3.2.1 | P1 |
| **FR-SEC-04** | Emit `X-Content-Type-Options: nosniff` by default (anti-MIME-sniff / anti-stored-XSS). | 9110 §8.3 (rationale); WHATWG Fetch | P1 |
| **NFR-STD-01** | Core targets HTTP/1.1 (9110/9111/9112); core TLS ALPN advertises only `http/1.1`. HTTP/2 (9113) and HTTP/3 (9114) are **optional opt-in tiers** (`docs/TRANSPORTS.md`), not core; `h2`/`h3` are advertised (ALPN/`Alt-Svc`) **only** when their tier is enabled. | 9113 §3.2/§4.3, 9114 §1.2/§2 | (constraint) |

---

## 6. Summary — the standards posture

servery is a **conformant HTTP/1.1 static origin server** under RFC 9110/9111/9112,
extending a stdlib base that is RFC 2616-era. It **inherits** correct
`Date`/`Content-Type`/`Content-Length`/`Last-Modified`/`If-Modified-Since` and the
gh-87389-hardened path translation, and **adds** the modern surface the base
lacks: Range/`206` (9110 §14), full conditional requests + `ETag` (9110 §13, §8.8),
`Cache-Control` (9111 §5.2.2), `Content-Disposition` UTF-8 filenames (6266/8187),
RFC 7617 Basic auth, HTTP/1.1 persistent connections (9112 §9.3), and the
`Host`-required `400` (9112 §3.2). HTTP/2 and HTTP/3 are **not part of this
zero-dependency core** — the core's TLS ALPN therefore advertises only
`http/1.1` — but they are **optional opt-in transport tiers**, not flatly
excluded (h2 is even pure-stdlib-feasible; h3 via `aioquic` or an experimental
`ctypes`→OpenSSL ≥ 3.5 backend); see `docs/TRANSPORTS.md`. Every MUST/SHOULD above
is paired with a cite and a test in §4.
