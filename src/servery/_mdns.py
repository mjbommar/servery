"""Minimal mDNS / DNS-SD responder (RFC 6762 + 6763), pure stdlib.

Advertises the running HTTP server as ``_http._tcp.local`` so it shows up in
Finder / file-manager network views and ``<host>.local`` resolves. Serves four
records — PTR, SRV, TXT, A — by announcing twice on startup and then answering
matching queries on the multicast group.

Deliberately minimal: no probing/conflict resolution (RFC 6762 §8.1) — fine for a
short-lived dev share; the instance name carries the port to reduce collisions.
Runs in a daemon thread; :func:`start` returns a handle with ``stop()``.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading

from servery import _log

_GROUP = "224.0.0.251"
_PORT = 5353
_TTL_HOST = 120  # A / SRV — host-name-bearing records (RFC 6762 §10)
_TTL_OTHER = 4500  # PTR / TXT — 75 minutes
_FLAGS_RESPONSE = 0x8400  # QR=1 (response) + AA=1 (authoritative)
_CLASS_IN = 0x0001
_CLASS_FLUSH = 0x8001  # IN + cache-flush bit (RFC 6762 §10.2) for unique records
_TYPE_A, _TYPE_PTR, _TYPE_TXT, _TYPE_SRV, _TYPE_ANY = 1, 12, 16, 33, 255
_SERVICE = "_http._tcp.local"


def _encode_name(name: str) -> bytes:
    """Encode a DNS name as length-prefixed labels (no compression — simple + valid)."""
    out = bytearray()
    for label in name.rstrip(".").split("."):
        out += bytes([len(label)]) + label.encode("utf-8")
    return bytes(out) + b"\x00"


def _record(name: str, rtype: int, rclass: int, ttl: int, rdata: bytes) -> bytes:
    return _encode_name(name) + struct.pack("!HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name; return (name, offset-past-it)."""
    labels: list[str] = []
    advanced = offset
    jumped = False
    while True:
        if offset >= len(data):
            break
        length = data[offset]
        if length & 0xC0 == 0xC0:  # compression pointer
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                advanced = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        if length == 0:
            break
        labels.append(data[offset : offset + length].decode("latin-1"))
        offset += length
    return ".".join(labels).lower(), (advanced if jumped else offset)


def build_answer(instance: str, host: str, ip: str, port: int, *, goodbye: bool = False) -> bytes:
    """The full PTR+SRV+TXT+A response packet. ``goodbye`` sets TTL 0 (withdraw)."""
    instance_fqdn = f"{instance}.{_SERVICE}"
    host_fqdn = f"{host}.local"
    th, to = (0, 0) if goodbye else (_TTL_HOST, _TTL_OTHER)
    txt = b"\x06path=/"  # one non-empty TXT string (RFC 6763 §6.1)
    records = [
        _record(_SERVICE, _TYPE_PTR, _CLASS_IN, to, _encode_name(instance_fqdn)),
        _record(
            instance_fqdn,
            _TYPE_SRV,
            _CLASS_FLUSH,
            th,
            struct.pack("!HHH", 0, 0, port) + _encode_name(host_fqdn),
        ),
        _record(instance_fqdn, _TYPE_TXT, _CLASS_FLUSH, to, txt),
        _record(host_fqdn, _TYPE_A, _CLASS_FLUSH, th, socket.inet_aton(ip)),
    ]
    header = struct.pack("!HHHHHH", 0, _FLAGS_RESPONSE, 0, len(records), 0, 0)
    return header + b"".join(records)


def _questions(data: bytes) -> list[tuple[str, int]]:
    """Parse the question section into (name, qtype) pairs (best-effort)."""
    try:
        qdcount = struct.unpack_from("!H", data, 4)[0]
    except struct.error:
        return []
    offset = 12
    out: list[tuple[str, int]] = []
    for _ in range(qdcount):
        name, offset = _read_name(data, offset)
        if offset + 4 > len(data):
            break
        qtype = struct.unpack_from("!H", data, offset)[0]
        offset += 4
        out.append((name, qtype))
    return out


class _Responder:
    def __init__(self, instance: str, host: str, ip: str, port: int) -> None:
        self._instance, self._host, self._ip, self._port = instance, host, ip, port
        self._instance_fqdn = f"{instance}.{_SERVICE}".lower()
        self._host_fqdn = f"{host}.local".lower()
        self._answer = build_answer(instance, host, ip, port)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def _open(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(AttributeError, OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", _PORT))
        mreq = socket.inet_aton(_GROUP) + socket.inet_aton(self._ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(1.0)
        return sock

    def start(self) -> None:
        self._sock = self._open()
        self._thread = threading.Thread(target=self._run, name="servery-mdns", daemon=True)
        self._thread.start()

    def _send(self, payload: bytes, dest: tuple[str, int]) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.sendto(payload, dest)

    def _run(self) -> None:
        sock = self._sock
        if sock is None:
            return
        # Announce twice ~1 s apart (RFC 6762 §8.3) so we appear promptly.
        for _ in range(2):
            if self._stop.is_set():
                return
            self._send(self._answer, (_GROUP, _PORT))
            self._stop.wait(1.0)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except (TimeoutError, OSError):
                continue
            if self._matches(data):
                # Unicast back to a legacy querier (source port != 5353), else multicast.
                dest = addr if addr[1] != _PORT else (_GROUP, _PORT)
                self._send(self._answer, dest)

    def _matches(self, data: bytes) -> bool:
        for name, qtype in _questions(data):
            if name == _SERVICE and qtype in (_TYPE_PTR, _TYPE_ANY):
                return True
            if name in (self._instance_fqdn, self._host_fqdn):
                return True
        return False

    def stop(self) -> None:
        self._stop.set()
        # Goodbye (TTL 0) so caches drop us promptly (RFC 6762 §10.1).
        self._send(
            build_answer(self._instance, self._host, self._ip, self._port, goodbye=True),
            (_GROUP, _PORT),
        )
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._sock is not None:
            self._sock.close()


def start(instance: str, host: str, ip: str, port: int) -> _Responder | None:
    """Start advertising; return a handle (with ``stop()``) or None if it can't bind."""
    responder = _Responder(instance, host, ip, port)
    try:
        responder.start()
    except OSError as exc:  # e.g. 5353 unavailable, no multicast route
        _log.logger.debug("mDNS responder could not start: %r", exc)
        return None
    return responder
