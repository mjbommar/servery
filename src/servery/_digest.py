"""Integrity digests for file responses (RFC 9530, Digest Fields).

A client that wants to verify a download — especially one reassembled from several
``Range`` requests fetched in parallel — sends ``Want-Repr-Digest`` and servery
answers with a ``Repr-Digest`` over the *full representation* (the identity file
bytes), independent of any range served. This is the standardized, self-describing
replacement for an out-of-band ``.sha256`` sidecar; it obsoletes the ambiguous
RFC 3230 ``Digest`` and the removed ``Content-MD5`` (RFC 7231).

Pure stdlib (``hashlib`` + ``base64``). The digest is computed only when the client
asks (it requires reading the whole file), so the default download path is untouched.

The wire value is an RFC 8941 dictionary whose members are byte sequences:
``Repr-Digest: sha-256=:<base64>:`` (lowercase algorithm key, padded base64 wrapped
in colons). ``sha-256`` and ``sha-512`` are offered; the legacy ``md5`` / ``sha``
keys RFC 9530 deprecates are never produced.
"""

from __future__ import annotations

import base64
import hashlib

_CHUNK = 256 * 1024

#: RFC 9530 algorithm key -> hashlib constructor name. Strongest-preferred order
#: is the iteration order here (used to break preference ties).
SUPPORTED: dict[str, str] = {"sha-256": "sha256", "sha-512": "sha512"}


def _parse_preferences(want: str) -> dict[str, float]:
    """Parse a ``Want-*-Digest`` value into ``{algorithm: preference}`` (RFC 8941-ish).

    Tolerant of the common shapes: ``sha-256=10`` (integer preference), a bare
    ``sha-256`` (wanted), and the boolean ``?0``/``?1`` forms. A value that cannot be
    read is treated as "wanted" (preference 1) — advisory, never an error.
    """
    prefs: dict[str, float] = {}
    for member in want.split(","):
        member = member.strip()
        if not member:
            continue
        name, sep, raw = member.partition("=")
        name = name.strip().lower()
        if not name:
            continue
        if not sep:
            prefs[name] = 1.0  # bare key — wanted
            continue
        raw = raw.strip()
        if raw == "?0":
            prefs[name] = 0.0
        elif raw == "?1":
            prefs[name] = 1.0
        else:
            try:
                prefs[name] = float(raw)
            except ValueError:
                prefs[name] = 1.0
    return prefs


def choose_algorithm(want: str | None) -> str | None:
    """Pick the digest algorithm to emit for a ``Want-Repr-Digest`` value, or ``None``.

    Returns the RFC 9530 key (``"sha-256"`` / ``"sha-512"``) of the supported
    algorithm with the highest positive preference (ties broken by :data:`SUPPORTED`
    order), or ``None`` when the client asked for nothing we support — or did not ask
    at all (``want`` is ``None``).
    """
    if want is None:
        return None
    prefs = _parse_preferences(want)
    candidates = [(prefs[name], name) for name in SUPPORTED if prefs.get(name, 0.0) > 0.0]
    if not candidates:
        return None
    best = max(prefs[name] for _pref, name in candidates)
    # Tie-break by SUPPORTED order (sha-256 first) among the top-preference algorithms.
    for name in SUPPORTED:
        if prefs.get(name, 0.0) == best:
            return name
    return None  # pragma: no cover - unreachable (best came from a SUPPORTED member)


def field_value(algorithm: str, data: bytes) -> str:
    """Build a ``Repr-Digest`` field value for ``data`` (in-memory representations)."""
    digest = hashlib.new(SUPPORTED[algorithm], data).digest()
    return _format(algorithm, digest)


def field_value_for_file(path: str, algorithm: str) -> str | None:
    """Build a ``Repr-Digest`` field value by streaming ``path``, or ``None`` on error.

    The file is hashed in bounded chunks, so memory stays flat regardless of size.
    """
    hasher = hashlib.new(SUPPORTED[algorithm])
    try:
        with open(path, "rb") as handle:  # noqa: PTH123 - os-level, mirrors the handler
            while chunk := handle.read(_CHUNK):
                hasher.update(chunk)
    except OSError:
        return None
    return _format(algorithm, hasher.digest())


def _format(algorithm: str, digest: bytes) -> str:
    encoded = base64.b64encode(digest).decode("ascii")
    return f"{algorithm}=:{encoded}:"
