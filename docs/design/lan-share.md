# Design: frictionless LAN share (QR + LAN-IP + mDNS)

Status: implementing. Pure stdlib (socket/struct only). The "run it, scan it,
you're in" moment — `servery --qr --discoverable`.

## Part 1 — LAN IPv4 detection (`_netinfo.py`)
Pick the address a phone on the same Wi-Fi would use, even when bound to `0.0.0.0`.
- The canonical trick: a `SOCK_DGRAM` socket `connect(("8.8.8.8", 80))` (sends **no**
  packets — it's a routing-table lookup) then `getsockname()[0]`.
- Returns `(ip, status)` where status ∈ {ok, loopback, offline}; treat `""`,
  `0.0.0.0`, `127.*` as not-ok. `gethostbyname(gethostname())` is **avoided** (Debian
  maps the hostname to `127.0.1.1`).
- Caveat: a full-tunnel VPN steals the default route → returns the VPN IP. Bind to a
  loopback address ⇒ phones can't reach it ⇒ warn, no QR.

## Part 2 — QR encoder (`_qr.py`), pure stdlib
Encode a URL → QR matrix → terminal half-blocks (`▀▄█` + space; 2 vertical modules
per char). Modeled on Nayuki `qrcodegen.py`'s structure.
- **Byte mode** (mixed-case URLs), **ECC level L** (scanned close-up, like miniserve),
  **versions 1–10** (≤271 bytes — covers any LAN URL; raise above that).
- Reed–Solomon over GF(2⁸), primitive poly `0x11D`: exp/log tables → generator poly →
  LFSR division for EC codewords.
- Assembly: mode `0100` + 8/16-bit count + data + terminator + `0xEC/0x11` pad; split
  into blocks per the v1–10 L table, RS-encode, interleave data then EC; append
  remainder bits (v1=0, v2–6=7, v7–10=0).
- Matrix: finders+separators, timing, alignment (centre table v2–10), dark module,
  format info (BCH 15,5, mask `0x5412`), version info (BCH 18,6 for v7–10), zig-zag
  data placement skipping function modules.
- **Masking**: all 8 patterns scored by penalty rules N1–N4; lowest wins (spec-correct,
  cheap).
- **Testing**: compare the generated matrix bit-for-bit against `segno` (pure-Python,
  TEST-ONLY oracle in the `test` group) across versions {1,2,3,7,10} × all 8 masks,
  plus unit tests for the RS codewords (known vectors) and the GF tables.

## Part 3 — mDNS / DNS-SD responder (`_mdns.py`), pure stdlib
`--discoverable` advertises `_http._tcp.local` so the server appears in Finder/file
managers and `hostname.local` resolves (RFC 6762 + 6763).
- Group `224.0.0.251:5353`; `SO_REUSEADDR`/`SO_REUSEPORT`, `IP_ADD_MEMBERSHIP`,
  multicast+IP TTL = 255 (RFC 6762 §11).
- Records: **PTR** `_http._tcp.local → <instance>._http._tcp.local` (class `0x0001`,
  TTL 4500), **SRV** instance → `<host>.local`:port (class `0x8001` cache-flush, TTL
  120), **TXT** `path=/` (class `0x8001`, TTL 4500), **A** `<host>.local → LAN IP`
  (class `0x8001`, TTL 120). Name compression for the shared suffix.
- Behavior: announce twice ~1 s apart on startup (§8.3), then a receive loop answering
  PTR/SRV/TXT/A queries (multicast, or unicast for QU/legacy non-5353 source); goodbye
  (TTL 0) on shutdown. **Probing is skipped** (ephemeral dev server) — instance name
  disambiguated with the port; documented tradeoff.
- Runs in a daemon thread; lifecycle tied to the server.

## CLI
- `--qr` — print a QR of the LAN URL on startup (needs a reachable LAN IP).
- `--discoverable` — start the mDNS responder.
Both are off by default and surfaced in the startup banner.
