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
from unittest import mock

from servery.config import Config
from servery.http2 import connection, frames, hpack
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
        Path(self._tmp.name, "big.bin").write_bytes(b"x" * 100_000)
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

    def test_malformed_request_without_method_is_reset(self):
        # Missing :method is malformed (RFC 9113 §8.3.1) -> RST_STREAM, never 200.
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                block = client.encoder.encode(
                    [(b":path", b"/f.txt"), (b":scheme", b"http"), (b":authority", b"x")]
                )
                flags = Flag.END_HEADERS | Flag.END_STREAM
                client.request(
                    1, raw=frames.build_header9(len(block), FrameType.HEADERS, flags, 1) + block
                )
                got_reset = False
                client.sock.settimeout(5)
                for _ in range(10):
                    data = client.sock.recv(65536)
                    if not data:
                        break
                    client.reader.feed(data)
                    if any(
                        isinstance(f, frames.RstStreamFrame) and f.stream_id == 1
                        for f in client.reader
                    ):
                        got_reset = True
                        break
                self.assertTrue(got_reset)
            finally:
                client.close()

    def test_active_stream_limit_is_advertised_and_enforced(self):
        cfg = Config.create(
            self._tmp.name,
            host="127.0.0.1",
            port=0,
            quiet=True,
            http2=True,
            max_h2_streams=1,
        )
        with serving(cfg) as (host, port):
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
                # Stream 1 remains remote-open, so it counts even before dispatch.
                client.request(
                    1,
                    raw=frames.build_header9(len(block), FrameType.HEADERS, Flag.END_HEADERS, 1)
                    + block,
                )
                client.request(3)
                seen_settings = False
                seen_header_limit = False
                refused = False
                for frame in _receive_frames(
                    client,
                    lambda item: isinstance(item, frames.RstStreamFrame) and item.stream_id == 3,
                ):
                    if isinstance(frame, frames.SettingsFrame):
                        seen_settings |= (
                            frames.SettingsParameter.MAX_CONCURRENT_STREAMS,
                            1,
                        ) in frame.settings
                        seen_header_limit |= (
                            frames.SettingsParameter.MAX_HEADER_LIST_SIZE,
                            connection._MAX_HEADER_LIST,
                        ) in frame.settings
                    if isinstance(frame, frames.RstStreamFrame) and frame.stream_id == 3:
                        refused = frame.error_code == frames.ErrorCode.REFUSED_STREAM
                        if refused:
                            break
                self.assertTrue(seen_settings)
                self.assertTrue(seen_header_limit)
                self.assertTrue(refused)
            finally:
                client.close()

    def test_stream_id_reuse_is_connection_error(self):
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                client.request(1)
                self.assertEqual(client.collect({1}), {1: 200})
                client.request(1)
                frames_seen = _receive_frames(
                    client, lambda frame: isinstance(frame, frames.GoAwayFrame)
                )
                goaway = next(
                    frame for frame in frames_seen if isinstance(frame, frames.GoAwayFrame)
                )
                self.assertEqual(goaway.error_code, frames.ErrorCode.PROTOCOL_ERROR)
            finally:
                client.close()

    def test_interleaved_continuation_is_connection_error(self):
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
                split = max(1, len(block) // 2)
                raw = (
                    frames.build_header9(split, FrameType.HEADERS, Flag.END_STREAM, 1)
                    + block[:split]
                    + frames.build_header9(8, FrameType.PING, Flag(0), 0)
                    + b"blocked!"
                )
                client.request(1, raw=raw)
                seen = _receive_frames(client, lambda frame: isinstance(frame, frames.GoAwayFrame))
                goaway = next(frame for frame in seen if isinstance(frame, frames.GoAwayFrame))
                self.assertEqual(goaway.error_code, frames.ErrorCode.PROTOCOL_ERROR)
            finally:
                client.close()

    def test_continuation_frame_flood_is_budgeted(self):
        with (
            mock.patch.object(connection, "_MAX_CONTINUATION_FRAMES", 1),
            serving(self.cfg) as (host, port),
        ):
            client = _H2Client(host, port)
            try:
                block = client.encoder.encode([(b":method", b"GET"), (b":path", b"/f.txt")])
                raw = (
                    frames.serialize(frames.HeadersFrame(1, Flag(0), block))
                    + frames.build_header9(0, FrameType.CONTINUATION, Flag(0), 1)
                    + frames.build_header9(0, FrameType.CONTINUATION, Flag(0), 1)
                )
                client.request(1, raw=raw)
                seen = _receive_frames(client, lambda frame: isinstance(frame, frames.GoAwayFrame))
                goaway = next(frame for frame in seen if isinstance(frame, frames.GoAwayFrame))
                self.assertEqual(goaway.error_code, frames.ErrorCode.ENHANCE_YOUR_CALM)
            finally:
                client.close()

    def test_settings_and_ping_flood_share_a_control_budget(self):
        with (
            mock.patch.object(connection, "_MAX_CONTROL_FRAMES", 2),
            serving(self.cfg) as (host, port),
        ):
            client = _H2Client(host, port)
            try:
                client.sock.sendall(
                    frames.serialize(frames.PingFrame(0, Flag(0), b"12345678"))
                    + frames.serialize(frames.PingFrame(0, Flag(0), b"abcdefgh"))
                )
                seen = _receive_frames(client, lambda frame: isinstance(frame, frames.GoAwayFrame))
                goaway = next(frame for frame in seen if isinstance(frame, frames.GoAwayFrame))
                self.assertEqual(goaway.error_code, frames.ErrorCode.ENHANCE_YOUR_CALM)
            finally:
                client.close()

    def test_rapid_reset_flood_is_budgeted(self):
        with (
            mock.patch.object(connection, "_MAX_RST_STREAMS", 1),
            serving(self.cfg) as (host, port),
        ):
            client = _H2Client(host, port)
            try:
                block = client.encoder.encode([(b":method", b"GET"), (b":path", b"/f.txt")])
                raw = bytearray()
                for stream_id in (1, 3):
                    raw += frames.serialize(frames.HeadersFrame(stream_id, Flag.END_HEADERS, block))
                    raw += frames.serialize(
                        frames.RstStreamFrame(stream_id, Flag(0), frames.ErrorCode.CANCEL)
                    )
                client.request(1, raw=bytes(raw))
                seen = _receive_frames(client, lambda frame: isinstance(frame, frames.GoAwayFrame))
                goaway = next(frame for frame in seen if isinstance(frame, frames.GoAwayFrame))
                self.assertEqual(goaway.error_code, frames.ErrorCode.ENHANCE_YOUR_CALM)
            finally:
                client.close()

    def test_connection_window_overflow_is_rejected(self):
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                client.sock.sendall(
                    frames.serialize(frames.WindowUpdateFrame(0, Flag(0), 0x7FFFFFFF))
                )
                seen = _receive_frames(client, lambda frame: isinstance(frame, frames.GoAwayFrame))
                goaway = next(frame for frame in seen if isinstance(frame, frames.GoAwayFrame))
                self.assertEqual(goaway.error_code, frames.ErrorCode.FLOW_CONTROL_ERROR)
            finally:
                client.close()

    def test_future_stream_control_frames_are_connection_errors(self):
        controls = (
            frames.WindowUpdateFrame(1, Flag(0), 1),
            frames.RstStreamFrame(1, Flag(0), frames.ErrorCode.CANCEL),
            frames.DataFrame(1, Flag.END_STREAM, b"x"),
        )
        for control in controls:
            with self.subTest(frame=type(control).__name__), serving(self.cfg) as (host, port):
                client = _H2Client(host, port)
                try:
                    client.sock.sendall(frames.serialize(control))
                    seen = _receive_frames(
                        client, lambda frame: isinstance(frame, frames.GoAwayFrame)
                    )
                    goaway = next(frame for frame in seen if isinstance(frame, frames.GoAwayFrame))
                    self.assertEqual(goaway.error_code, frames.ErrorCode.PROTOCOL_ERROR)
                finally:
                    client.close()

    def test_data_on_closed_stream_gets_stream_closed_reset(self):
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                client.request(1)
                self.assertEqual(client.collect({1}), {1: 200})
                client.sock.sendall(frames.serialize(frames.DataFrame(1, Flag.END_STREAM, b"x")))
                seen = _receive_frames(
                    client,
                    lambda frame: isinstance(frame, frames.RstStreamFrame) and frame.stream_id == 1,
                )
                reset = next(frame for frame in seen if isinstance(frame, frames.RstStreamFrame))
                self.assertEqual(reset.error_code, frames.ErrorCode.STREAM_CLOSED)
            finally:
                client.close()

    def test_active_stream_window_overflow_is_rejected(self):
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                block = client.encoder.encode([(b":method", b"GET"), (b":path", b"/f.txt")])
                client.request(
                    1,
                    raw=frames.build_header9(len(block), FrameType.HEADERS, Flag.END_HEADERS, 1)
                    + block,
                )
                client.sock.sendall(
                    frames.serialize(frames.WindowUpdateFrame(1, Flag(0), 0x7FFFFFFF))
                )
                seen = _receive_frames(client, lambda frame: isinstance(frame, frames.GoAwayFrame))
                goaway = next(frame for frame in seen if isinstance(frame, frames.GoAwayFrame))
                self.assertEqual(goaway.error_code, frames.ErrorCode.FLOW_CONTROL_ERROR)
            finally:
                client.close()

    def test_initial_window_change_adjusts_active_stream(self):
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                client.sock.sendall(
                    frames.serialize(
                        frames.SettingsFrame(
                            0,
                            Flag(0),
                            ((frames.SettingsParameter.INITIAL_WINDOW_SIZE, 10),),
                        )
                    )
                )
                client.request(1, "/big.bin")
                first = _receive_data(client, 1, 10)
                self.assertEqual(len(first), 10)
                client.sock.sendall(
                    frames.serialize(
                        frames.SettingsFrame(
                            0,
                            Flag(0),
                            ((frames.SettingsParameter.INITIAL_WINDOW_SIZE, 20),),
                        )
                    )
                )
                second = _receive_data(client, 1, 10)
                self.assertEqual(len(second), 10)
            finally:
                client.close()

    def test_small_stream_finishes_while_large_stream_is_flow_limited(self):
        with serving(self.cfg) as (host, port):
            client = _H2Client(host, port)
            try:
                client.sock.sendall(
                    frames.serialize(
                        frames.SettingsFrame(
                            0,
                            Flag(0),
                            ((frames.SettingsParameter.INITIAL_WINDOW_SIZE, 16384),),
                        )
                    )
                )
                client.request(1, "/big.bin")
                client.request(3, "/f.txt")
                seen = _receive_frames(
                    client,
                    lambda frame: (
                        isinstance(frame, frames.DataFrame)
                        and frame.stream_id == 3
                        and frame.end_stream
                    ),
                )
                self.assertTrue(
                    any(
                        isinstance(frame, frames.DataFrame)
                        and frame.stream_id == 3
                        and frame.end_stream
                        for frame in seen
                    )
                )
                self.assertFalse(
                    any(
                        isinstance(frame, frames.DataFrame)
                        and frame.stream_id == 1
                        and frame.end_stream
                        for frame in seen
                    )
                )
            finally:
                client.close()


def _receive_frames(client: _H2Client, done) -> list[frames.Frame]:
    seen: list[frames.Frame] = []
    client.sock.settimeout(5)
    while True:
        data = client.sock.recv(65536)
        if not data:
            return seen
        client.reader.feed(data)
        for frame in client.reader:
            seen.append(frame)
            if done(frame):
                return seen


def _receive_data(client: _H2Client, stream_id: int, count: int) -> bytes:
    body = bytearray()
    client.sock.settimeout(5)
    while len(body) < count:
        data = client.sock.recv(65536)
        if not data:
            break
        client.reader.feed(data)
        for frame in client.reader:
            if isinstance(frame, frames.DataFrame) and frame.stream_id == stream_id:
                body += frame.data
    return bytes(body)


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
