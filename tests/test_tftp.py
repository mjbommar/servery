"""TFTP server tests (RFC 1350 + the RFC 2347-9 options)."""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from servery import _tftp

_RRQ, _WRQ, _DATA, _ACK, _ERROR, _OACK = 1, 2, 3, 4, 5, 6


class TftpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"tftp error {code}: {message}")
        self.code = code


# --- pure-function unit tests --------------------------------------------


class ParseRequestTest(unittest.TestCase):
    def test_plain(self):
        parsed = _tftp._parse_request(b"file.bin\x00octet\x00")
        self.assertEqual(parsed, ("file.bin", "octet", {}))

    def test_with_options(self):
        parsed = _tftp._parse_request(b"f\x00octet\x00blksize\x001024\x00tsize\x000\x00")
        self.assertEqual(parsed, ("f", "octet", {"blksize": "1024", "tsize": "0"}))

    def test_malformed(self):
        self.assertIsNone(_tftp._parse_request(b"\x00octet\x00"))
        self.assertIsNone(_tftp._parse_request(b"only"))


class NetasciiTest(unittest.TestCase):
    def test_roundtrip(self):
        # netascii canonicalizes line endings, so the roundtrip is an identity only
        # for local text that has no literal CRLF (a bare CR/LF is the local form).
        for original in (b"a\nb\nc", b"plain", b"trailing\n", b"cr\rmid"):
            self.assertEqual(_tftp.from_netascii(_tftp.to_netascii(original)), original)

    def test_lf_becomes_crlf(self):
        self.assertEqual(_tftp.to_netascii(b"a\nb"), b"a\r\nb")

    def test_crlf_decodes_to_lf(self):
        self.assertEqual(_tftp.from_netascii(b"a\r\nb"), b"a\nb")


class NegotiateTest(unittest.TestCase):
    def test_clamps_blksize(self):
        self.assertEqual(
            _tftp._negotiate({"blksize": "99999"}, tsize_value=None)["blksize"], "65464"
        )
        self.assertEqual(_tftp._negotiate({"blksize": "2"}, tsize_value=None)["blksize"], "8")

    def test_tsize_reported_for_read(self):
        self.assertEqual(_tftp._negotiate({"tsize": "0"}, tsize_value=1234)["tsize"], "1234")

    def test_unknown_options_dropped(self):
        self.assertEqual(_tftp._negotiate({"frob": "1"}, tsize_value=None), {})

    def test_garbage_blksize_falls_back(self):
        self.assertEqual(
            _tftp._negotiate({"blksize": "abc"}, tsize_value=None)["blksize"],
            str(_tftp.DEFAULT_BLOCK),
        )

    def test_timeout_option(self):
        self.assertEqual(_tftp._negotiate({"timeout": "5"}, tsize_value=None)["timeout"], "5")
        # Out of range (1-255) is not echoed.
        self.assertNotIn("timeout", _tftp._negotiate({"timeout": "999"}, tsize_value=None))
        # Garbage falls back to the default timeout (in range), so it is echoed.
        self.assertEqual(
            _tftp._negotiate({"timeout": "x"}, tsize_value=None)["timeout"],
            str(int(_tftp._DEFAULT_TIMEOUT)),
        )


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.srv = _tftp.TftpServer.__new__(_tftp.TftpServer)  # no socket needed

    def test_default(self):
        self.assertEqual(self.srv._resolve({}), (_tftp.DEFAULT_BLOCK, _tftp._DEFAULT_TIMEOUT))

    def test_timeout_parsed(self):
        self.assertEqual(self.srv._resolve({"timeout": "7"})[1], 7.0)

    def test_timeout_garbage_and_out_of_range_default(self):
        self.assertEqual(self.srv._resolve({"timeout": "x"})[1], _tftp._DEFAULT_TIMEOUT)
        self.assertEqual(self.srv._resolve({"timeout": "9999"})[1], _tftp._DEFAULT_TIMEOUT)


# --- a tiny in-test TFTP client over real UDP -----------------------------


def _build_request(opcode: int, filename: str, mode: str, options: dict | None) -> bytes:
    body = filename.encode() + b"\x00" + mode.encode() + b"\x00"
    for key, value in (options or {}).items():
        body += f"{key}\x00{value}\x00".encode()
    return struct.pack("!H", opcode) + body


def _parse_oack(packet: bytes) -> dict[str, str]:
    """Parse an OACK body (key\\0 val\\0 ...) — no filename/mode prefix."""
    fields = packet[2:].split(b"\x00")
    options: dict[str, str] = {}
    for i in range(0, len(fields) - 1, 2):
        if fields[i] == b"":
            break
        options[fields[i].decode("latin-1")] = fields[i + 1].decode("latin-1")
    return options


def tftp_get(host, port, filename, *, mode="octet", options=None, timeout=2.0) -> bytes:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(_build_request(_RRQ, filename, mode, options), (host, port))
        blksize = 512
        server_tid = None
        if options:  # expect an OACK, then ACK block 0
            packet, server_tid = sock.recvfrom(70000)
            op = struct.unpack("!H", packet[:2])[0]
            if op == _ERROR:
                raise _error(packet)
            blksize = int(_parse_oack(packet).get("blksize", blksize))
            sock.sendto(struct.pack("!HH", _ACK, 0), server_tid)
        data = bytearray()
        expected = 1
        while True:
            packet, addr = sock.recvfrom(blksize + 4)
            server_tid = server_tid or addr
            op, block = struct.unpack("!HH", packet[:4])
            if op == _ERROR:
                raise _error(packet)
            assert op == _DATA and block == expected, (op, block, expected)
            payload = packet[4:]
            data += payload
            sock.sendto(struct.pack("!HH", _ACK, block), server_tid)
            if len(payload) < blksize:
                return bytes(data)
            expected = (expected + 1) & 0xFFFF
    finally:
        sock.close()


def tftp_put(host, port, filename, data, *, mode="octet", options=None, timeout=2.0) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(_build_request(_WRQ, filename, mode, options), (host, port))
        packet, server_tid = sock.recvfrom(70000)
        op = struct.unpack("!H", packet[:2])[0]
        if op == _ERROR:
            raise _error(packet)
        blksize = 512
        if op == _OACK:
            blksize = int(_parse_oack(packet).get("blksize", blksize))
        block = 1
        offset = 0
        while True:
            chunk = data[offset : offset + blksize]
            offset += len(chunk)
            sock.sendto(struct.pack("!HH", _DATA, block) + chunk, server_tid)
            ack, _ = sock.recvfrom(100)
            aop, ablock = struct.unpack("!HH", ack[:4])
            if aop == _ERROR:
                raise _error(ack)
            assert aop == _ACK and ablock == block, (aop, ablock, block)
            if len(chunk) < blksize:
                return
            block = (block + 1) & 0xFFFF
    finally:
        sock.close()


def _error(packet: bytes) -> TftpError:
    code = struct.unpack("!H", packet[2:4])[0]
    message = packet[4:].split(b"\x00", 1)[0].decode("latin-1", "replace")
    return TftpError(code, message)


def _raw_reply(host, port, packet: bytes, timeout: float = 2.0) -> bytes:
    """Send a raw datagram to the main port and return the first reply."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (host, port))
        return sock.recvfrom(70000)[0]
    finally:
        sock.close()


@contextlib.contextmanager
def _running(
    root: str | os.PathLike[str], *, allow_write=False, max_write_size=100 * 1024 * 1024
) -> Iterator[_tftp.TftpServer]:
    server = _tftp.TftpServer(
        os.path.realpath(root),
        "127.0.0.1",
        0,
        allow_write=allow_write,
        max_write_size=max_write_size,
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


class ReadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_octet_small(self):
        (self.root / "hello.txt").write_bytes(b"hello tftp")
        with _running(self.root) as srv:
            self.assertEqual(tftp_get("127.0.0.1", srv.port, "hello.txt"), b"hello tftp")

    def test_octet_multiblock(self):
        data = bytes(range(256)) * 8  # 2048 bytes -> 4 full blocks + an empty terminator
        (self.root / "big.bin").write_bytes(data)
        with _running(self.root) as srv:
            self.assertEqual(tftp_get("127.0.0.1", srv.port, "big.bin"), data)

    def test_exact_multiple_of_blocksize(self):
        data = b"z" * 512  # exactly one block -> needs a trailing empty DATA
        (self.root / "exact.bin").write_bytes(data)
        with _running(self.root) as srv:
            self.assertEqual(tftp_get("127.0.0.1", srv.port, "exact.bin"), data)

    def test_blksize_option(self):
        data = b"q" * 3000
        (self.root / "opt.bin").write_bytes(data)
        with _running(self.root) as srv:
            got = tftp_get("127.0.0.1", srv.port, "opt.bin", options={"blksize": "1024"})
        self.assertEqual(got, data)

    def test_netascii_translation(self):
        (self.root / "text.txt").write_bytes(b"a\nb\nc")
        with _running(self.root) as srv:
            got = tftp_get("127.0.0.1", srv.port, "text.txt", mode="netascii")
        self.assertEqual(got, b"a\r\nb\r\nc")

    def test_not_found(self):
        with _running(self.root) as srv, self.assertRaises(TftpError) as ctx:
            tftp_get("127.0.0.1", srv.port, "missing.bin")
        self.assertEqual(ctx.exception.code, 1)

    def test_directory_is_not_a_file(self):
        (self.root / "sub").mkdir()
        with _running(self.root) as srv, self.assertRaises(TftpError) as ctx:
            tftp_get("127.0.0.1", srv.port, "sub")
        self.assertEqual(ctx.exception.code, 1)

    def test_traversal_blocked(self):
        # A path trying to escape the root must not read an outside file.
        with _running(self.root) as srv, self.assertRaises(TftpError):
            tftp_get("127.0.0.1", srv.port, "../../../../etc/hostname")

    def test_all_options_negotiated(self):
        data = b"k" * 2500
        (self.root / "opt.bin").write_bytes(data)
        opts = {"blksize": "1024", "tsize": "0", "timeout": "3"}
        with _running(self.root) as srv:
            got = tftp_get("127.0.0.1", srv.port, "opt.bin", options=opts)
        self.assertEqual(got, data)

    def test_unsupported_mode(self):
        (self.root / "f.txt").write_bytes(b"hi")
        request = struct.pack("!H", _RRQ) + b"f.txt\x00mail\x00"
        with _running(self.root) as srv:
            reply = _raw_reply("127.0.0.1", srv.port, request)
        self.assertEqual(struct.unpack("!H", reply[:2])[0], _ERROR)
        self.assertEqual(struct.unpack("!H", reply[2:4])[0], 4)  # illegal operation

    def test_malformed_request(self):
        request = struct.pack("!H", _RRQ) + b"no-nul-terminator"
        with _running(self.root) as srv:
            reply = _raw_reply("127.0.0.1", srv.port, request)
        self.assertEqual(struct.unpack("!H", reply[:2])[0], _ERROR)


class WriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_disabled_by_default(self):
        with _running(self.root, allow_write=False) as srv, self.assertRaises(TftpError) as ctx:
            tftp_put("127.0.0.1", srv.port, "new.bin", b"data")
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((self.root / "new.bin").exists())

    def test_write_octet(self):
        data = bytes(range(256)) * 6  # multiblock
        with _running(self.root, allow_write=True) as srv:
            tftp_put("127.0.0.1", srv.port, "up.bin", data)
        self.assertEqual((self.root / "up.bin").read_bytes(), data)

    def test_write_missing_parent(self):
        with _running(self.root, allow_write=True) as srv, self.assertRaises(TftpError) as ctx:
            tftp_put("127.0.0.1", srv.port, "nope/deep.bin", b"data")
        self.assertEqual(ctx.exception.code, 1)  # directory not found

    def test_write_existing_refused(self):
        (self.root / "there.bin").write_bytes(b"old")
        with _running(self.root, allow_write=True) as srv, self.assertRaises(TftpError) as ctx:
            tftp_put("127.0.0.1", srv.port, "there.bin", b"new")
        self.assertEqual(ctx.exception.code, 6)
        self.assertEqual((self.root / "there.bin").read_bytes(), b"old")

    def test_write_size_cap(self):
        with _running(self.root, allow_write=True, max_write_size=5) as srv:
            with self.assertRaises(TftpError) as ctx:
                tftp_put("127.0.0.1", srv.port, "toobig.bin", b"x" * 50)
            self.assertEqual(ctx.exception.code, 3)
        self.assertFalse((self.root / "toobig.bin").exists())

    def test_write_with_options(self):
        data = b"w" * 2500  # multiblock with a negotiated blksize
        with _running(self.root, allow_write=True) as srv:
            tftp_put(
                "127.0.0.1",
                srv.port,
                "opt.bin",
                data,
                options={"blksize": "1024", "tsize": str(len(data))},
            )
        self.assertEqual((self.root / "opt.bin").read_bytes(), data)

    def test_write_netascii(self):
        # The wire CRLF must be stored as the local LF.
        with _running(self.root, allow_write=True) as srv:
            tftp_put("127.0.0.1", srv.port, "text.txt", b"a\r\nb", mode="netascii")
        self.assertEqual((self.root / "text.txt").read_bytes(), b"a\nb")


if __name__ == "__main__":
    unittest.main()
