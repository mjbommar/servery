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
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import BinaryIO

_CHUNK = 256 * 1024

#: RFC 9530 algorithm key -> hashlib constructor name. Strongest-preferred order
#: is the iteration order here (used to break preference ties).
SUPPORTED: dict[str, str] = {"sha-256": "sha256", "sha-512": "sha512"}

type CacheKey = tuple[str, int, int, int, int, int, str]


class _Flight:
    """One transient same-identity digest shared by concurrent callers."""

    __slots__ = ("error", "event", "references", "value")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.references = 0
        self.value: str | None = None
        self.error: BaseException | None = None


class DigestCache:
    """Thread-safe entry-bounded LRU with same-key miss coalescing.

    A zero-sized cache retains no digest after the concurrent callers finish, but
    still prevents a burst of requests for one file identity from hashing it once
    per connection. Retained caching is deliberately opt-in because metadata-based
    invalidation has the same coarse-filesystem caveats as ETags.
    """

    def __init__(self, max_entries: int = 0) -> None:
        if max_entries < 0:
            raise ValueError("max_entries cannot be negative")
        self.max_entries = max_entries
        self._items: OrderedDict[CacheKey, str] = OrderedDict()
        self._flights: dict[CacheKey, _Flight] = {}
        self._lock = threading.Lock()

    @property
    def current_entries(self) -> int:
        with self._lock:
            return len(self._items)

    def get(self, key: CacheKey) -> str | None:
        if self.max_entries <= 0:
            return None
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: CacheKey, value: str) -> None:
        if self.max_entries <= 0:
            return
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = value
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def get_or_compute(self, key: CacheKey, factory: Callable[[], str]) -> str:
        """Return a cached value or compute it without a same-key hash stampede."""
        cached = self.get(key)
        if cached is not None:
            return cached
        with self._lock:
            flight = self._flights.get(key)
            owner = flight is None
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
            flight.references += 1
        try:
            if owner:
                cached = self.get(key)
                try:
                    value = cached if cached is not None else factory()
                    if cached is None:
                        self.put(key, value)
                except BaseException as exc:
                    flight.error = exc
                    flight.event.set()
                    raise
                flight.value = value
                flight.event.set()
                return value
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            if flight.value is None:  # pragma: no cover - event/result invariant
                raise RuntimeError("digest flight completed without a result")
            return flight.value
        finally:
            with self._lock:
                flight.references -= 1
                if flight.references == 0:
                    self._flights.pop(key, None)


def cache_key(path: str, stat: os.stat_result, algorithm: str) -> CacheKey:
    """Key a digest by pathname and the identity facts used for the response."""
    return (
        os.path.realpath(path),
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
        algorithm,
    )


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
    try:
        with open(path, "rb") as handle:  # noqa: PTH123 - os-level, mirrors the handler
            return field_value_for_handle(handle, algorithm)
    except OSError:
        return None


def field_value_for_handle(
    handle: BinaryIO,
    algorithm: str,
    expected_size: int | None = None,
) -> str:
    """Hash one opened identity in bounded chunks and restore its file position.

    When ``expected_size`` is supplied, exactly that many bytes must still be
    readable. This makes truncation fail before response headers are emitted and
    hashes the same byte extent the response plan will send if a file grows.
    """
    position = handle.tell()
    hasher = hashlib.new(SUPPORTED[algorithm])
    remaining = expected_size
    try:
        handle.seek(0)
        while remaining is None or remaining > 0:
            amount = _CHUNK if remaining is None else min(_CHUNK, remaining)
            chunk = handle.read(amount)
            if not chunk:
                if remaining:
                    raise OSError("file changed while hashing the planned representation")
                break
            hasher.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    finally:
        handle.seek(position)
    return _format(algorithm, hasher.digest())


def _format(algorithm: str, digest: bytes) -> str:
    encoded = base64.b64encode(digest).decode("ascii")
    return f"{algorithm}=:{encoded}:"
