"""TFTP server (RFC 1350) — an opt-in, LAN-only file-transfer tier over UDP.

TFTP is the small, still-living protocol that nothing modern has replaced for its
niche: **PXE network boot**, and pushing firmware / configs to switches, routers,
phones, and other embedded gear that only speaks it. It is trivial in pure stdlib
(``socket`` + ``struct``), so it fits servery's zero-dependency core, but it is the
opposite of safe-by-default: **no authentication, no encryption, cleartext UDP**,
and a known DDoS-amplification surface. It is therefore strictly opt-in
(``--tftp``), read-only unless ``--tftp-write`` is added, and meant for trusted
LAN / lab networks only — never the open internet.

It runs as a **separate UDP listener** alongside the HTTP server (like the mDNS
advertiser), serving the *same* directory. Path safety reuses the one HTTP-agnostic
choke-point, :func:`servery.security.safe_join`, so a request can never escape the
served root. Octet (binary) and netascii (text) modes are supported, plus the
RFC 2347-2349 ``blksize`` / ``tsize`` / ``timeout`` options that PXE relies on.

Implements: RRQ/WRQ, DATA/ACK lockstep with timeout-retransmit, ERROR packets,
and OACK option negotiation. Each transfer runs on its own ephemeral socket (TID)
in a worker thread, matching the RFC's connection model.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import tempfile
import threading
from typing import BinaryIO

from servery import _log, _writecoord, security

# Opcodes (RFC 1350 §5) and the OACK extension (RFC 2347).
_RRQ, _WRQ, _DATA, _ACK, _ERROR, _OACK = 1, 2, 3, 4, 5, 6

# Error codes (RFC 1350 §5.3 / RFC 2347).
_ERR_UNDEFINED = 0
_ERR_NOT_FOUND = 1
_ERR_ACCESS = 2
_ERR_DISK_FULL = 3
_ERR_ILLEGAL = 4
_ERR_UNKNOWN_TID = 5
_ERR_EXISTS = 6
_ERR_BAD_OPTIONS = 8

DEFAULT_BLOCK = 512
MIN_BLOCK = 8  # RFC 2348
MAX_BLOCK = 65464  # RFC 2348 ceiling (65535 - 4-byte header - rounding)
_DEFAULT_TIMEOUT = 2.0  # seconds to wait for each ACK/DATA before retransmit
_RETRIES = 5
_RECV_BUFFER = MAX_BLOCK + 4


# --- packet build / parse -------------------------------------------------


def _pack_data(block: int, payload: bytes) -> bytes:
    return struct.pack("!HH", _DATA, block) + payload


def _pack_ack(block: int) -> bytes:
    return struct.pack("!HH", _ACK, block)


def _pack_error(code: int, message: str) -> bytes:
    return struct.pack("!HH", _ERROR, code) + message.encode("latin-1", "replace") + b"\x00"


def _pack_oack(options: dict[str, str]) -> bytes:
    body = b"".join(
        key.encode("latin-1") + b"\x00" + value.encode("latin-1") + b"\x00"
        for key, value in options.items()
    )
    return struct.pack("!H", _OACK) + body


def _parse_request(payload: bytes) -> tuple[str, str, dict[str, str]] | None:
    """Parse an RRQ/WRQ body: ``filename\\0 mode\\0 [opt\\0 val\\0]...``."""
    fields = payload.split(b"\x00")
    # A well-formed request has filename, mode, then option pairs, then a trailing
    # empty field from the final NUL.
    if len(fields) < 3 or fields[0] == b"":
        return None
    filename = fields[0].decode("latin-1")
    mode = fields[1].decode("latin-1").lower()
    options: dict[str, str] = {}
    rest = fields[2:-1] if fields[-1] == b"" else fields[2:]
    for i in range(0, len(rest) - 1, 2):
        options[rest[i].decode("latin-1").lower()] = rest[i + 1].decode("latin-1")
    return filename, mode, options


# --- netascii translation (RFC 764 / RFC 1350 §A) -------------------------


def to_netascii(data: bytes) -> bytes:
    """Encode local bytes to netascii: bare ``\\n`` -> ``\\r\\n``, bare ``\\r`` -> ``\\r\\0``."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        byte = data[i]
        if byte == 0x0D:  # CR
            out += b"\r"
            nxt = data[i + 1] if i + 1 < n else None
            out += b"\n" if nxt == 0x0A else b"\x00"
            i += 2 if nxt == 0x0A else 1
        elif byte == 0x0A:  # bare LF
            out += b"\r\n"
            i += 1
        else:
            out.append(byte)
            i += 1
    return bytes(out)


def from_netascii(data: bytes) -> bytes:
    """Decode netascii to local bytes: ``\\r\\n`` -> ``\\n``, ``\\r\\0`` -> ``\\r``."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        byte = data[i]
        if byte == 0x0D:  # CR
            nxt = data[i + 1] if i + 1 < n else None
            if nxt == 0x0A:
                out += b"\n"
                i += 2
            elif nxt == 0x00:
                out += b"\r"
                i += 2
            else:
                out += b"\r"
                i += 1
        else:
            out.append(byte)
            i += 1
    return bytes(out)


class _NetasciiEncoder:
    def __init__(self) -> None:
        self._pending_cr = False

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        out = bytearray()
        offset = 0
        if self._pending_cr:
            if data.startswith(b"\n"):
                out += b"\r\n"
                offset = 1
            else:
                out += b"\r\0"
            self._pending_cr = False
        while offset < len(data):
            byte = data[offset]
            if byte == 0x0D:
                if offset + 1 >= len(data):
                    self._pending_cr = True
                    offset += 1
                elif data[offset + 1] == 0x0A:
                    out += b"\r\n"
                    offset += 2
                else:
                    out += b"\r\0"
                    offset += 1
            elif byte == 0x0A:
                out += b"\r\n"
                offset += 1
            else:
                out.append(byte)
                offset += 1
        if final and self._pending_cr:
            out += b"\r\0"
            self._pending_cr = False
        return bytes(out)


class _NetasciiDecoder:
    def __init__(self) -> None:
        self._pending_cr = False

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        out = bytearray()
        for byte in data:
            if self._pending_cr:
                if byte == 0x0A:
                    out += b"\n"
                    self._pending_cr = False
                    continue
                if byte == 0x00:
                    out += b"\r"
                    self._pending_cr = False
                    continue
                out += b"\r"
                self._pending_cr = False
            if byte == 0x0D:
                self._pending_cr = True
            else:
                out.append(byte)
        if final and self._pending_cr:
            out += b"\r"
            self._pending_cr = False
        return bytes(out)


class _NetasciiReader:
    """Stream a local file as netascii without materializing the representation."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source
        self._encoder = _NetasciiEncoder()
        self._buffer = bytearray()
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            while not self._eof:
                self._fill()
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        while len(self._buffer) < size and not self._eof:
            self._fill()
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _fill(self) -> None:
        raw = self._source.read(64 * 1024)
        if raw:
            self._buffer += self._encoder.feed(raw)
        else:
            self._buffer += self._encoder.feed(b"", final=True)
            self._eof = True

    def wire_size(self) -> int:
        position = self._source.tell()
        self._source.seek(0)
        encoder = _NetasciiEncoder()
        total = 0
        while raw := self._source.read(64 * 1024):
            total += len(encoder.feed(raw))
        total += len(encoder.feed(b"", final=True))
        self._source.seek(position)
        return total

    def close(self) -> None:
        self._source.close()


type _ReadSource = BinaryIO | _NetasciiReader


def _negotiate(options: dict[str, str], *, tsize_value: int | None) -> dict[str, str]:
    """Pick the subset of client options we accept, in OACK reply form."""
    accepted: dict[str, str] = {}
    if "blksize" in options:
        try:
            blksize = int(options["blksize"])
        except ValueError:
            blksize = DEFAULT_BLOCK
        accepted["blksize"] = str(max(MIN_BLOCK, min(MAX_BLOCK, blksize)))
    if "timeout" in options:
        try:
            timeout = int(options["timeout"])
        except ValueError:
            timeout = int(_DEFAULT_TIMEOUT)
        if 1 <= timeout <= 255:
            accepted["timeout"] = str(timeout)
    if "tsize" in options and tsize_value is not None:
        # RRQ: we report the file size. WRQ: the client states it; echo it back.
        accepted["tsize"] = str(tsize_value)
    return accepted


class TftpServer:
    """A read (and optionally write) TFTP server over UDP, bound to a directory."""

    def __init__(
        self,
        root_real: str,
        host: str,
        port: int,
        *,
        allow_write: bool = False,
        max_write_size: int = 100 * 1024 * 1024,
        max_transfers: int = 32,
        target_locks: _writecoord.TargetLocks | None = None,
        write_lock_timeout: float = 0.0,
    ) -> None:
        self.root_real = root_real
        self.allow_write = allow_write
        self.max_write_size = max_write_size
        self.max_transfers = max_transfers
        self.target_locks = target_locks or _writecoord.TargetLocks()
        self.write_lock_timeout = write_lock_timeout
        self._transfer_slots = threading.BoundedSemaphore(max_transfers)
        self._family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self._sock = socket.socket(self._family, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self.host = host
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, name="servery-tftp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    # --- main accept loop ------------------------------------------------

    def _serve(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                payload, addr = self._sock.recvfrom(_RECV_BUFFER)
            except TimeoutError:
                continue
            except OSError:
                break  # socket closed by stop()
            if len(payload) < 2 or struct.unpack("!H", payload[:2])[0] not in (_RRQ, _WRQ):
                continue
            if not self._transfer_slots.acquire(blocking=False):
                self._send_to(addr, _pack_error(_ERR_UNDEFINED, "server busy"))
                continue
            threading.Thread(
                target=self._dispatch_limited, args=(payload, addr), daemon=True
            ).start()

    def _dispatch_limited(self, payload: bytes, addr: tuple) -> None:
        try:
            self._dispatch(payload, addr)
        finally:
            self._transfer_slots.release()

    def _dispatch(self, payload: bytes, addr: tuple) -> None:
        if len(payload) < 2:
            return
        opcode = struct.unpack("!H", payload[:2])[0]
        if opcode not in (_RRQ, _WRQ):
            return  # stray DATA/ACK to the main port — ignore
        parsed = _parse_request(payload[2:])
        if parsed is None:
            self._send_to(addr, _pack_error(_ERR_ILLEGAL, "malformed request"))
            return
        filename, mode, options = parsed
        if mode not in ("octet", "netascii"):
            self._send_to(addr, _pack_error(_ERR_ILLEGAL, f"unsupported mode {mode!r}"))
            return
        try:
            if opcode == _RRQ:
                self._handle_read(addr, filename, mode, options)
            else:
                self._handle_write(addr, filename, mode, options)
        except OSError as exc:  # pragma: no cover - transient socket/disk error
            _log.logger.debug("tftp transfer aborted: %r", exc)

    # --- read (RRQ) ------------------------------------------------------

    def _handle_read(self, addr: tuple, filename: str, mode: str, options: dict) -> None:
        fs_path = security.safe_join(self.root_real, filename)
        if fs_path is None:
            self._send_to(addr, _pack_error(_ERR_ACCESS, "access violation"))
            return
        if not os.path.isfile(fs_path):
            self._send_to(addr, _pack_error(_ERR_NOT_FOUND, "file not found"))
            return
        try:
            data = self._read_source(fs_path, mode)
        except OSError:
            self._send_to(addr, _pack_error(_ERR_NOT_FOUND, "file not found"))
            return
        with self._transfer_socket() as sock:
            blksize, timeout = self._resolve(options)
            if options:
                accepted = _negotiate(options, tsize_value=self._size_of(data))
                blksize = int(accepted.get("blksize", blksize))
                if accepted and not self._send_and_wait_ack(
                    sock, addr, _pack_oack(accepted), 0, timeout
                ):
                    return
            self._send_blocks(sock, addr, data, blksize, timeout)
        with contextlib.suppress(OSError):
            data.close()

    def _read_source(self, fs_path: str, mode: str) -> _ReadSource:
        if mode == "netascii":
            return _NetasciiReader(open(fs_path, "rb"))
        return open(fs_path, "rb")

    @staticmethod
    def _size_of(data: _ReadSource) -> int | None:
        if isinstance(data, _NetasciiReader):
            return data.wire_size()
        try:
            current = data.tell()
            size = data.seek(0, os.SEEK_END)
            data.seek(current)
            return size
        except OSError:  # pragma: no cover - non-seekable
            return None

    def _send_blocks(
        self, sock: socket.socket, addr: tuple, data: _ReadSource, blksize: int, timeout: float
    ) -> None:
        block = 1
        while True:
            payload = data.read(blksize)
            if not self._send_and_wait_ack(sock, addr, _pack_data(block, payload), block, timeout):
                return
            if len(payload) < blksize:
                return  # a short (incl. empty) block ends the transfer
            block = (block + 1) & 0xFFFF

    def _send_and_wait_ack(
        self, sock: socket.socket, addr: tuple, packet: bytes, block: int, timeout: float
    ) -> bool:
        """Send ``packet`` and wait for ACK ``block``, retransmitting on timeout."""
        for _ in range(_RETRIES + 1):
            sock.sendto(packet, addr)
            reply = self._recv_from(sock, addr, timeout)
            if reply is None:
                continue
            opcode = struct.unpack("!H", reply[:2])[0]
            if opcode == _ERROR:
                return False
            if opcode == _ACK and struct.unpack("!H", reply[2:4])[0] == block:
                return True
        return False

    # --- write (WRQ) -----------------------------------------------------

    def _handle_write(self, addr: tuple, filename: str, mode: str, options: dict) -> None:
        if not self.allow_write:
            self._send_to(addr, _pack_error(_ERR_ACCESS, "writes are disabled"))
            return
        fs_path = security.safe_join(self.root_real, filename)
        if fs_path is None or os.path.basename(fs_path) == "":
            self._send_to(addr, _pack_error(_ERR_ACCESS, "access violation"))
            return
        if not os.path.isdir(os.path.dirname(fs_path)):
            self._send_to(addr, _pack_error(_ERR_NOT_FOUND, "directory not found"))
            return
        with self.target_locks.hold(fs_path, self.write_lock_timeout) as acquired:
            if not acquired:
                self._send_to(addr, _pack_error(_ERR_EXISTS, "another write is active"))
                return
            if os.path.exists(fs_path):
                self._send_to(addr, _pack_error(_ERR_EXISTS, "file already exists"))
                return
            with self._transfer_socket() as sock:
                blksize, timeout = self._resolve(options)
                accepted = _negotiate(options, tsize_value=self._wrq_tsize(options))
                blksize = int(accepted.get("blksize", blksize))
                reply = _pack_oack(accepted) if accepted else _pack_ack(0)
                self._receive_blocks(sock, addr, fs_path, mode, blksize, timeout, reply)

    def _wrq_tsize(self, options: dict) -> int | None:
        if "tsize" not in options:
            return None
        try:
            return int(options["tsize"])
        except ValueError:
            return None

    def _receive_blocks(
        self,
        sock: socket.socket,
        addr: tuple,
        fs_path: str,
        mode: str,
        blksize: int,
        timeout: float,
        first_reply: bytes,
    ) -> None:
        directory = os.path.dirname(fs_path)
        tmp = tempfile.NamedTemporaryFile(dir=directory, delete=False)  # noqa: SIM115
        total = 0
        reply = first_reply
        expected = 1
        committed = False
        decoder = _NetasciiDecoder() if mode == "netascii" else None
        try:
            while True:
                packet = self._send_and_wait_data(sock, addr, reply, expected, timeout)
                if packet is None:
                    return  # gave up or client aborted
                payload = packet
                total += len(payload)
                if total > self.max_write_size:
                    sock.sendto(_pack_error(_ERR_DISK_FULL, "upload exceeds the size limit"), addr)
                    return
                last = len(payload) < blksize
                tmp.write(decoder.feed(payload, final=last) if decoder is not None else payload)
                reply = _pack_ack(expected)
                expected = (expected + 1) & 0xFFFF
                if last:
                    sock.sendto(reply, addr)  # final ACK
                    tmp.close()
                    os.replace(tmp.name, fs_path)
                    committed = True
                    return
        finally:
            if not committed:
                with contextlib.suppress(OSError):
                    tmp.close()
                with contextlib.suppress(OSError):
                    os.unlink(tmp.name)

    def _send_and_wait_data(
        self, sock: socket.socket, addr: tuple, reply: bytes, expected: int, timeout: float
    ) -> bytes | None:
        """Send ``reply`` (ACK/OACK) and wait for DATA ``expected``; return its payload."""
        for _ in range(_RETRIES + 1):
            sock.sendto(reply, addr)
            packet = self._recv_from(sock, addr, timeout)
            if packet is None:
                continue
            opcode = struct.unpack("!H", packet[:2])[0]
            if opcode == _ERROR:
                return None
            if opcode == _DATA and struct.unpack("!H", packet[2:4])[0] == expected:
                return packet[4:]
            # A duplicate of the previous block (or noise) — resend our last reply.
        return None

    # --- shared helpers --------------------------------------------------

    def _transfer_socket(self) -> socket.socket:
        sock = socket.socket(self._family, socket.SOCK_DGRAM)
        # Bind the same address the listener uses, on a fresh ephemeral port — the
        # per-transfer TID (RFC 1350 §4). Replies then originate from an IP the
        # client can route back to, and we inherit the operator's bind choice.
        sock.bind((self.host, 0))
        return sock

    def _recv_from(self, sock: socket.socket, addr: tuple, timeout: float) -> bytes | None:
        """Receive one datagram from ``addr``; reject a different TID (RFC 1350 §4)."""
        sock.settimeout(timeout)
        try:
            packet, source = sock.recvfrom(_RECV_BUFFER)
        except (TimeoutError, OSError):
            return None
        if source[:2] != addr[:2]:  # compare (host, port) — a stray peer
            sock.sendto(_pack_error(_ERR_UNKNOWN_TID, "unknown transfer id"), source)
            return None
        if len(packet) < 4:
            return None
        return packet

    def _resolve(self, options: dict) -> tuple[int, float]:
        timeout = _DEFAULT_TIMEOUT
        if "timeout" in options:
            try:
                value = int(options["timeout"])
                if 1 <= value <= 255:
                    timeout = float(value)
            except ValueError:
                pass
        return DEFAULT_BLOCK, timeout

    def _send_to(self, addr: tuple, packet: bytes) -> None:
        with self._transfer_socket() as sock:
            sock.sendto(packet, addr)
