"""HTTP/2 conformance for the supported surface (RFC 9113) + httpx TLS interop.

servery ships a *minimal* HTTP/2 server (GET/HEAD). It is HPACK- and framing-
correct (h2spec ``generic`` 50/52, ``hpack`` 8/8 against h2spec 2.6.0) and
interops with curl and httpx over real TLS+ALPN. It does NOT implement the full
strict protocol-error state machine of h2spec's ``http2`` suite — these tests
cover what is supported and guard against regressions.
"""

from __future__ import annotations

import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from servery.config import Config
from servery.http2 import frames, hpack
from servery.http2.frames import Flag, FrameType
from servery.server import make_server
from tests._harness import serving

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


class _H2Client:
    """A tiny h2c client over servery's own codec for conformance probing."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.sendall(
            frames.CONNECTION_PREFACE + frames.serialize(frames.SettingsFrame(0, Flag(0), ()))
        )
        self.encoder = hpack.Encoder()
        self.decoder = hpack.Decoder()
        self.reader = frames.FrameReader()

    def request(self, stream_id: int, path: str = "/f.txt", *, raw: bytes | None = None) -> None:
        if raw is not None:
            self.sock.sendall(raw)
            return
        block = self.encoder.encode(
            [
                (b":method", b"GET"),
                (b":path", path.encode("ascii")),
                (b":scheme", b"http"),
                (b":authority", b"x"),
            ]
        )
        flags = Flag.END_HEADERS | Flag.END_STREAM
        self.sock.sendall(
            frames.build_header9(len(block), FrameType.HEADERS, flags, stream_id) + block
        )

    def collect(self, stream_ids: set[int]) -> dict[int, int]:
        statuses: dict[int, int] = {}
        ended: set[int] = set()
        while ended < stream_ids:
            data = self.sock.recv(65536)
            if not data:
                break
            self.reader.feed(data)
            for frame in self.reader:
                if isinstance(frame, frames.HeadersFrame) and frame.stream_id in stream_ids:
                    for name, value in self.decoder.decode(frame.header_block):
                        if name == b":status":
                            statuses[frame.stream_id] = int(value)
                    if frame.end_stream:
                        ended.add(frame.stream_id)
                elif isinstance(frame, frames.DataFrame) and frame.end_stream:
                    ended.add(frame.stream_id)
        return statuses

    def close(self) -> None:
        self.sock.close()


class Http2ConformanceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "f.txt").write_text("data")
        self.cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True, http2=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_padded_headers_accepted(self):
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                block = client.encoder.encode(
                    [
                        (b":method", b"GET"),
                        (b":path", b"/f.txt"),
                        (b":scheme", b"http"),
                        (b":authority", b"x"),
                    ]
                )
                pad = 6
                payload = bytes([pad]) + block + (b"\x00" * pad)
                flags = Flag.END_HEADERS | Flag.END_STREAM | Flag.PADDED
                client.request(
                    1, raw=frames.build_header9(len(payload), FrameType.HEADERS, flags, 1) + payload
                )
                self.assertEqual(client.collect({1}), {1: 200})
            finally:
                client.close()

    def test_hpack_dynamic_table_across_requests(self):
        # One encoder across two requests on one connection: the second reuses the
        # dynamic table, so the server's decoder must maintain state across frames.
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                client.request(1)
                client.request(3)
                self.assertEqual(client.collect({1, 3}), {1: 200, 3: 200})
            finally:
                client.close()

    def test_concurrent_streams(self):
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                for sid in (1, 3, 5):
                    client.request(sid)
                self.assertEqual(client.collect({1, 3, 5}), {1: 200, 3: 200, 5: 200})
            finally:
                client.close()


def _make_cert(directory: Path) -> tuple[str, str] | None:
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return str(cert), str(key)


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class Http2TlsInteropTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        directory = Path(self._tmp.name)
        directory.joinpath("f.txt").write_text("over tls")
        pair = _make_cert(directory)
        if pair is None:
            self._tmp.cleanup()
            self.skipTest("openssl not available")
        cert, key = pair
        config = Config.create(
            directory, host="127.0.0.1", port=0, quiet=True, http2=True, tls_cert=cert, tls_key=key
        )
        self.httpd = make_server(config)
        self.host = str(self.httpd.server_address[0])
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()

    def test_httpx_negotiates_h2_over_tls(self):
        with httpx.Client(http2=True, verify=False) as client:
            resp = client.get(f"https://{self.host}:{self.port}/f.txt")
        self.assertEqual(resp.http_version, "HTTP/2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "over tls")


if __name__ == "__main__":
    unittest.main()
