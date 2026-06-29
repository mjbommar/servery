"""On-the-fly content-coding negotiation (RFC 9110 §8.4.1.3 / §12.5.3).

Two codings, both pure stdlib: ``gzip`` (always available) and ``zstd`` (the
``compression.zstd`` module added in Python 3.14, PEP 784). When the interpreter
has zstd *and* the client accepts it, zstd wins (better ratio, much faster
decode); otherwise we fall back to gzip. ``deflate`` is skipped — it is ambiguous
(raw vs zlib wrapper, §8.4.1.2 note) — and ``br`` (brotli) needs a third-party
dependency, so it stays out of the zero-dependency core forever.

zstd is advertised *only* when the running interpreter actually has it, so a 3.13
build never claims a coding it cannot produce (RFC 9110 §12.5.5).
"""

from __future__ import annotations

import gzip

try:  # zstd landed in the stdlib in 3.14 (PEP 784); absent on 3.13.
    from compression import zstd as _zstd  # ty: ignore[unresolved-import]
except ImportError:  # pragma: no cover - exercised on 3.13, not the 3.14 CI default
    _zstd = None

#: True when this interpreter can produce ``Content-Encoding: zstd`` (3.14+).
HAVE_ZSTD = _zstd is not None
#: zstd compression level for on-the-fly encoding — a balance of ratio vs CPU,
#: well clear of the slow high end (max is 22) so it stays cheap per request.
ZSTD_LEVEL = 9

# Don't bother below the gzip framing overhead (~18 B); don't buffer-compress
# above the cap (serve identity + sendfile instead — bounds per-request memory,
# and large files are usually already-compressed media anyway).
GZIP_MIN = 1024
GZIP_MAX = 10 * 1024 * 1024

# Compress these (an allowlist: text-like and not already compressed). Anything
# not matched — jpeg/png/webp, mp4, zip/gz, woff/woff2, … — is served as-is.
_COMPRESSIBLE_EXACT = frozenset(
    {
        "application/json",
        "application/javascript",
        "text/javascript",
        "application/xml",
        "text/xml",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/wasm",
        "application/manifest+json",
        "application/x-ndjson",
        "application/rss+xml",
        "application/atom+xml",
        "application/ld+json",
        "font/ttf",
        "font/otf",
        "application/vnd.ms-fontobject",
    }
)


def compressible(content_type: str) -> bool:
    """True if ``content_type`` is worth gzipping (text-like, not pre-compressed)."""
    base = content_type.split(";", 1)[0].strip().lower()
    if base.startswith("text/"):
        return True
    if base.endswith(("+json", "+xml")):
        return True
    return base in _COMPRESSIBLE_EXACT


# Text-based types that aren't ``text/*`` but are still UTF-8 text on the wire.
_CHARSET_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "image/svg+xml",
        "application/manifest+json",
        "application/x-ndjson",
        "application/ld+json",
        "application/rss+xml",
        "application/atom+xml",
    }
)


def with_charset(content_type: str) -> str:
    """Add ``; charset=utf-8`` to a text-based type so browsers decode it as UTF-8.

    ``text/*`` historically defaults to US-ASCII (and browsers fall back to a legacy
    8-bit encoding), which mangles UTF-8 — em dashes, curly quotes, emoji — when no
    charset is declared (e.g. a Markdown or plain-text file with no in-band charset).
    servery serves UTF-8, so it says so. Types that already carry a parameter, and
    binary types, are returned unchanged.
    """
    if not content_type or ";" in content_type:
        return content_type
    base = content_type.strip().lower()
    if base.startswith("text/") or base.endswith(("+json", "+xml")) or base in _CHARSET_TYPES:
        return f"{content_type}; charset=utf-8"
    return content_type


def _accepts(accept_encoding: str, names: tuple[str, ...]) -> bool:
    """True if any of ``names`` is acceptable per RFC 9110 §12.5.3 (q-value aware).

    Handles ``coding;q=0`` (forbidden), ``*`` wildcards, and the empty header (which
    means "no coding"). An absent header arrives here as ``""`` → not accepted, the
    conservative choice (we only compress when the coding is explicitly welcome).
    """
    best_named = -1.0
    best_star = -1.0
    for token in accept_encoding.split(","):
        token = token.strip()
        if not token:
            continue
        name, _, params = token.partition(";")
        name = name.strip().lower()
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if name in names:
            best_named = max(best_named, quality)
        elif name == "*":
            best_star = quality
    if best_named >= 0:  # the coding is explicitly listed — its q-value decides
        return best_named > 0
    return best_star > 0  # else a non-zero wildcard accepts it


def accepts_gzip(accept_encoding: str) -> bool:
    """True if the client accepts ``gzip`` (q-value aware; ``x-gzip`` alias)."""
    return _accepts(accept_encoding, ("gzip", "x-gzip"))


def accepts_zstd(accept_encoding: str) -> bool:
    """True if the client accepts ``zstd`` (q-value aware)."""
    return _accepts(accept_encoding, ("zstd",))


def negotiate(accept_encoding: str, *, enabled: bool) -> str | None:
    """Pick the best content-coding for a generated body, or ``None`` for identity.

    Prefers ``zstd`` (when this interpreter has it and the client accepts it), then
    ``gzip``. Used for always-compressible generated HTML (the directory listing),
    which has no size/type gate.
    """
    if not enabled:
        return None
    if HAVE_ZSTD and accepts_zstd(accept_encoding):
        return "zstd"
    if accepts_gzip(accept_encoding):
        return "gzip"
    return None


def choose_encoding(
    content_type: str, size: int, accept_encoding: str, *, enabled: bool
) -> str | None:
    """The single content-coding decision for a file: ``"zstd"``, ``"gzip"``, or ``None``.

    ``size`` is the identity (uncompressed) length, so this can be answered from a
    stat without reading the file. Shared by every transport so the decision is one
    place (it must agree with the ETag's coding variant, RFC 9110 §8.8.3.3).
    """
    if not (enabled and compressible(content_type) and GZIP_MIN <= size <= GZIP_MAX):
        return None
    return negotiate(accept_encoding, enabled=True)


def should_gzip(content_type: str, size: int, accept_encoding: str, *, enabled: bool) -> bool:
    """Back-compat shim: does a file qualify for gzip? (See :func:`choose_encoding`.)"""
    return (
        enabled
        and compressible(content_type)
        and GZIP_MIN <= size <= GZIP_MAX
        and accepts_gzip(accept_encoding)
    )


def gzip_bytes(data: bytes) -> bytes:
    """Compress ``data`` as a gzip stream (deterministic: fixed mtime)."""
    return gzip.compress(data, compresslevel=6, mtime=0)


def zstd_bytes(data: bytes) -> bytes:
    """Compress ``data`` as a zstd frame. Only call when :data:`HAVE_ZSTD`."""
    if _zstd is None:  # defensive: callers gate on HAVE_ZSTD
        raise RuntimeError("zstd is unavailable (needs Python 3.14+)")
    return _zstd.compress(data, ZSTD_LEVEL)


def encode(data: bytes, coding: str) -> bytes:
    """Apply ``coding`` (``"zstd"`` or ``"gzip"``) to ``data``."""
    return zstd_bytes(data) if coding == "zstd" else gzip_bytes(data)
