"""End-to-end HTTP/2 (h2c prior-knowledge) tests.

The client is hand-rolled on servery's own HPACK + frame codec, so the test has
no third-party dependency and exercises the real server connection.
"""

import base64
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from servery.config import Config
from servery.http2 import frames, hpack
from servery.http2.frames import Flag, FrameType
from servery.server import make_server


def _h2_exchange(
    host: str,
    port: int,
    path: str,
    method: str = "GET",
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> tuple[int | None, dict[bytes, bytes], bytes]:
    sock = socket.create_connection((host, port), timeout=5)
    try:
        request = [
            (b":method", method.encode("ascii")),
            (b":path", path.encode("ascii")),
            (b":scheme", b"http"),
            (b":authority", b"localhost"),
            *extra_headers,
        ]
        block = hpack.Encoder().encode(request)
        out = bytearray(frames.CONNECTION_PREFACE)
        out += frames.serialize(frames.SettingsFrame(0, Flag(0), ()))
        out += frames.build_header9(
            len(block), FrameType.HEADERS, Flag.END_HEADERS | Flag.END_STREAM, 1
        )
        out += block
        sock.sendall(bytes(out))

        reader = frames.FrameReader()
        decoder = hpack.Decoder()
        status: int | None = None
        headers: dict[bytes, bytes] = {}
        body = bytearray()
        done = False
        while not done:
            data = sock.recv(65536)
            if not data:
                break
            reader.feed(data)
            for frame in reader:
                if isinstance(frame, frames.HeadersFrame) and frame.stream_id == 1:
                    for name, value in decoder.decode(frame.header_block):
                        if name == b":status":
                            status = int(value)
                        headers[name] = value
                    done = frame.end_stream
                elif isinstance(frame, frames.DataFrame) and frame.stream_id == 1:
                    body += frame.data
                    done = done or frame.end_stream
        return status, headers, bytes(body)
    finally:
        sock.close()


class _H2ServerCase(unittest.TestCase):
    auth: str | None = None
    max_buffered_response = 1024

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "hello.txt").write_text("hi there")
        (self.dir / "big.bin").write_bytes(b"x" * 20000)
        (self.dir / "sub").mkdir()
        config = Config.create(
            self.dir,
            host="127.0.0.1",
            port=0,
            quiet=True,
            http2=True,
            auth=self.auth,
            max_buffered_response=self.max_buffered_response,
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

    def _get(self, path, method="GET", extra_headers=()):
        status, _headers, body = _h2_exchange(self.host, self.port, path, method, extra_headers)
        return status, body

    def _get_full(self, path, method="GET", extra_headers=()):
        return _h2_exchange(self.host, self.port, path, method, extra_headers)


class Http2ServerTest(_H2ServerCase):
    def test_get_file(self):
        status, body = self._get("/hello.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hi there")

    def test_file_has_validators(self):
        status, headers, _body = self._get_full("/hello.txt")
        self.assertEqual(status, 200)
        self.assertIn(b"etag", headers)  # buffered backend now sends validators
        self.assertIn(b"last-modified", headers)

    def test_conditional_304(self):
        _status, headers, _body = self._get_full("/hello.txt")
        etag = headers[b"etag"]
        status2, headers2, body2 = self._get_full(
            "/hello.txt", extra_headers=((b"if-none-match", etag),)
        )
        self.assertEqual(status2, 304)  # revalidated
        self.assertEqual(body2, b"")
        self.assertEqual(headers2[b"etag"], etag)

    def test_get_listing(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"hello.txt", body)

    def test_head_file(self):
        status, body = self._get("/hello.txt", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_head_streaming_file_closes_without_a_body(self):
        status, body = self._get("/big.bin", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_missing_is_404(self):
        status, _ = self._get("/does-not-exist")
        self.assertEqual(status, 404)

    def test_path_traversal_is_blocked(self):
        # Containment is enforced once (in translate_path, which returns "" for an
        # escaping path); _build_response trusts that. A "../" escape must 404 and
        # never leak a file outside the served root.
        secret = self.dir.parent / "h2_secret.txt"
        secret.write_text("SECRET")
        self.addCleanup(secret.unlink)
        for path in ("/../h2_secret.txt", "/%2e%2e/h2_secret.txt"):
            status, body = self._get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"SECRET", body)

    def test_large_file_spans_frames(self):
        status, body = self._get("/big.bin")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"x" * 20000)

    def test_directory_redirect(self):
        status, _ = self._get("/sub")
        self.assertEqual(status, 301)

    def test_post_not_allowed(self):
        status, _ = self._get("/hello.txt", method="POST")
        self.assertEqual(status, 405)

    def _expect_ping_ack(self, sock: socket.socket) -> bool:
        reader = frames.FrameReader()
        for _ in range(10):
            data = sock.recv(65536)
            if not data:
                return False
            reader.feed(data)
            if any(isinstance(f, frames.PingFrame) and f.ack for f in reader):
                return True
        return False

    def test_ping_is_acked(self):
        sock = socket.create_connection((self.host, self.port), timeout=5)
        try:
            out = bytearray(frames.CONNECTION_PREFACE)
            out += frames.serialize(frames.SettingsFrame(0, Flag(0), ()))
            out += frames.build_header9(8, FrameType.PING, Flag(0), 0) + b"servery!"
            sock.sendall(bytes(out))
            self.assertTrue(self._expect_ping_ack(sock))
        finally:
            sock.close()

    def test_stream_flow_control(self):
        # Advertise a tiny per-stream initial window; the body only flows if the
        # server honors stream-level WINDOW_UPDATE replenishment.
        sock = socket.create_connection((self.host, self.port), timeout=5)
        try:
            block = hpack.Encoder().encode(
                [
                    (b":method", b"GET"),
                    (b":path", b"/big.bin"),
                    (b":scheme", b"http"),
                    (b":authority", b"x"),
                ]
            )
            out = bytearray(frames.CONNECTION_PREFACE)
            out += frames.serialize(
                frames.SettingsFrame(
                    0, Flag(0), ((frames.SettingsParameter.INITIAL_WINDOW_SIZE, 100),)
                )
            )
            out += frames.build_header9(
                len(block), FrameType.HEADERS, Flag.END_HEADERS | Flag.END_STREAM, 1
            )
            out += block
            sock.sendall(bytes(out))

            reader = frames.FrameReader()
            decoder = hpack.Decoder()
            body = bytearray()
            status = None
            done = False
            while not done:
                sock.sendall(frames.serialize(frames.WindowUpdateFrame(0, Flag(0), 65535)))
                sock.sendall(frames.serialize(frames.WindowUpdateFrame(1, Flag(0), 65535)))
                data = sock.recv(65536)
                if not data:
                    break
                reader.feed(data)
                for frame in reader:
                    if isinstance(frame, frames.HeadersFrame) and frame.stream_id == 1:
                        for name, value in decoder.decode(frame.header_block):
                            if name == b":status":
                                status = int(value)
                        done = frame.end_stream
                    elif isinstance(frame, frames.DataFrame) and frame.stream_id == 1:
                        body += frame.data
                        done = done or frame.end_stream
            self.assertEqual(status, 200)
            self.assertEqual(len(body), 20000)
        finally:
            sock.close()

    def test_window_update_and_rst_are_tolerated(self):
        sock = socket.create_connection((self.host, self.port), timeout=5)
        try:
            block = hpack.Encoder().encode(
                [
                    (b":method", b"GET"),
                    (b":path", b"/"),
                    (b":scheme", b"http"),
                    (b":authority", b"x"),
                ]
            )
            out = bytearray(frames.CONNECTION_PREFACE)
            out += frames.serialize(frames.SettingsFrame(0, Flag(0), ()))
            out += frames.serialize(frames.WindowUpdateFrame(0, Flag(0), 1000))
            out += frames.build_header9(len(block), FrameType.HEADERS, Flag.END_HEADERS, 1) + block
            out += frames.serialize(frames.RstStreamFrame(1, Flag(0), 0))
            out += frames.build_header9(8, FrameType.PING, Flag(0), 0) + b"alive!!!"
            sock.sendall(bytes(out))
            self.assertTrue(self._expect_ping_ack(sock))  # server survived the RST/WINDOW_UPDATE
        finally:
            sock.close()

    def test_drain_goaway_refuses_new_stream_and_completes_accepted_stream(self):
        sock = socket.create_connection((self.host, self.port), timeout=2)
        try:
            encoder = hpack.Encoder()
            request1 = encoder.encode(
                [
                    (b":method", b"GET"),
                    (b":path", b"/hello.txt"),
                    (b":scheme", b"http"),
                    (b":authority", b"x"),
                ]
            )
            out = bytearray(frames.CONNECTION_PREFACE)
            # Hold stream 1 after its response HEADERS so draining begins while
            # accepted response work is still live.
            out += frames.serialize(
                frames.SettingsFrame(
                    0,
                    Flag(0),
                    ((frames.SettingsParameter.INITIAL_WINDOW_SIZE, 0),),
                )
            )
            out += frames.build_header9(
                len(request1), FrameType.HEADERS, Flag.END_HEADERS | Flag.END_STREAM, 1
            )
            out += request1
            sock.sendall(bytes(out))

            reader = frames.FrameReader()
            saw_response_headers = False
            deadline = time.monotonic() + 2
            while not saw_response_headers and time.monotonic() < deadline:
                reader.feed(sock.recv(65536))
                saw_response_headers = any(
                    isinstance(frame, frames.HeadersFrame) and frame.stream_id == 1
                    for frame in reader
                )
            self.assertTrue(saw_response_headers)

            self.httpd.begin_draining()
            goaway = None
            deadline = time.monotonic() + 2
            while goaway is None and time.monotonic() < deadline:
                reader.feed(sock.recv(65536))
                goaway = next(
                    (frame for frame in reader if isinstance(frame, frames.GoAwayFrame)),
                    None,
                )
            self.assertIsNotNone(goaway)
            assert goaway is not None
            self.assertEqual(goaway.error_code, frames.ErrorCode.NO_ERROR)
            self.assertEqual(goaway.last_stream_id, 1)

            request3 = encoder.encode(
                [
                    (b":method", b"GET"),
                    (b":path", b"/hello.txt"),
                    (b":scheme", b"http"),
                    (b":authority", b"x"),
                ]
            )
            sock.sendall(
                frames.build_header9(
                    len(request3), FrameType.HEADERS, Flag.END_HEADERS | Flag.END_STREAM, 3
                )
                + request3
                + frames.serialize(frames.WindowUpdateFrame(1, Flag(0), 1024))
            )

            refused = False
            completed = False
            body = bytearray()
            deadline = time.monotonic() + 2
            while not (refused and completed) and time.monotonic() < deadline:
                data = sock.recv(65536)
                if not data:
                    break
                reader.feed(data)
                for frame in reader:
                    if isinstance(frame, frames.RstStreamFrame) and frame.stream_id == 3:
                        refused = frame.error_code == frames.ErrorCode.REFUSED_STREAM
                    elif isinstance(frame, frames.DataFrame) and frame.stream_id == 1:
                        body += frame.data
                        completed = frame.end_stream
            self.assertTrue(refused)
            self.assertTrue(completed)
            self.assertEqual(body, b"hi there")
        finally:
            sock.close()

    def test_idle_drain_sends_goaway_and_exits_without_deadline_wait(self):
        sock = socket.create_connection((self.host, self.port), timeout=2)
        try:
            sock.sendall(
                frames.CONNECTION_PREFACE + frames.serialize(frames.SettingsFrame(0, Flag(0), ()))
            )
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with self.httpd._drain_condition:
                    if self.httpd._connection_drainers:
                        break
                time.sleep(0.005)
            else:
                self.fail("HTTP/2 connection did not register its drain hook")

            started = time.monotonic()
            self.httpd.begin_draining()
            reader = frames.FrameReader()
            goaway = None
            saw_eof = False
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not saw_eof:
                data = sock.recv(65536)
                if not data:
                    saw_eof = True
                    break
                reader.feed(data)
                for frame in reader:
                    if isinstance(frame, frames.GoAwayFrame):
                        goaway = frame
            self.assertIsNotNone(goaway)
            assert goaway is not None
            self.assertEqual(goaway.last_stream_id, 0)
            self.assertEqual(goaway.error_code, frames.ErrorCode.NO_ERROR)
            self.assertTrue(saw_eof)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            sock.close()


class Http2AuthTest(_H2ServerCase):
    auth = "alice:secret"

    def test_401_without_credentials(self):
        status, _ = self._get("/hello.txt")
        self.assertEqual(status, 401)

    def test_200_with_credentials(self):
        token = base64.b64encode(b"alice:secret").decode("ascii")
        status, body = self._get(
            "/hello.txt", extra_headers=((b"authorization", f"Basic {token}".encode()),)
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hi there")


if __name__ == "__main__":
    unittest.main()
