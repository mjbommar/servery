"""On-the-fly gzip content-coding (RFC 9110 §8.4.1.3 / §12.5.3).

gzip only — ``deflate`` is ambiguous (raw vs zlib wrapper, §8.4.1.2 note) and
brotli needs a dependency. Pure stdlib (``gzip``/``zlib``).
"""

from __future__ import annotations

import gzip

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


def accepts_gzip(accept_encoding: str) -> bool:
    """True if the client accepts ``gzip`` per RFC 9110 §12.5.3 (q-value aware).

    Handles ``gzip;q=0`` (forbidden), ``*`` wildcards, and the empty header (which
    means "no coding"). An absent header arrives here as ``""`` → not accepted, the
    conservative choice (we only compress when gzip is explicitly welcome).
    """
    best_gzip = -1.0
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
        if name in ("gzip", "x-gzip"):
            best_gzip = quality
        elif name == "*":
            best_star = quality
    if best_gzip >= 0:  # gzip explicitly listed — its q-value decides
        return best_gzip > 0
    return best_star > 0  # else a non-zero wildcard accepts it


def should_gzip(content_type: str, size: int, accept_encoding: str, *, enabled: bool) -> bool:
    """The single gzip decision: enabled, compressible type, in the size band, accepted.

    ``size`` is the identity (uncompressed) length, so this can be answered from a
    stat without reading the file. Shared by every transport so the decision is one
    place (it must agree with the ETag's gzip variant, RFC 9110 §8.8.3.3).
    """
    return (
        enabled
        and compressible(content_type)
        and GZIP_MIN <= size <= GZIP_MAX
        and accepts_gzip(accept_encoding)
    )


def gzip_bytes(data: bytes) -> bytes:
    """Compress ``data`` as a gzip stream (deterministic: fixed mtime)."""
    return gzip.compress(data, compresslevel=6, mtime=0)
