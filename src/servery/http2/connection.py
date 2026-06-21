"""A minimal but correct HTTP/2 server connection (RFC 9113).

Serves GET/HEAD (files and directory listings) over a single HTTP/2 connection,
reusing servery's path-safety, listing, and auth. It speaks h2 over TLS (ALPN)
and h2c via prior-knowledge (cleartext). DATA is sent respecting the peer's
flow-control windows.

DoS mitigations: caps on concurrent streams, on buffered header-block size (plus
HPACK's own header-list guard), and a RST_STREAM budget (the "rapid reset"
CVE-2023-44487 class).

Range requests, conditional requests, and request bodies are not handled on the
HTTP/2 path yet; the HTTP/1.1 handler remains the full-featured path.
"""

from __future__ import annotations

import contextlib
import mimetypes
import os
from typing import TYPE_CHECKING

from servery import listing, security
from servery.http2 import frames, hpack
from servery.http2.frames import ErrorCode, Flag, FrameType

if TYPE_CHECKING:
    from servery.handler import ServeryHandler

_MAX_CONCURRENT_STREAMS = 100
_MAX_HEADER_BLOCK = 64 * 1024
_MAX_RST_STREAMS = 200
_OUR_MAX_FRAME = 16384

_HeaderList = list[tuple[bytes, bytes]]


class H2Connection:
    """Drives one HTTP/2 connection to completion."""

    def __init__(self, handler: ServeryHandler) -> None:
        self.handler = handler
        self.rfile = handler.rfile
        self.sock = handler.connection
        self.config = handler._server.config
        self.decoder = hpack.Decoder()
        self.encoder = hpack.Encoder()
        self.reader = frames.FrameReader(max_frame_size=_OUR_MAX_FRAME)
        self.blocks: dict[int, bytearray] = {}
        self.peer_window = frames.SETTINGS_DEFAULTS[frames.SettingsParameter.INITIAL_WINDOW_SIZE]
        self.conn_window = self.peer_window or 65535
        self.rst_count = 0
        self.running = True

    # -- main loop --------------------------------------------------------

    def run(self) -> None:
        if self._read_exact(len(frames.CONNECTION_PREFACE)) != frames.CONNECTION_PREFACE:
            return
        self._send_settings()
        try:
            while self.running:
                # read1: return whatever a single read yields (don't block for a
                # full buffer — the peer waits for our response before sending more).
                data = self.rfile.read1(65536)
                if not data:
                    break
                self.reader.feed(data)
                for frame in self.reader:
                    self._handle_frame(frame)
                    if not self.running:
                        break
        except (OSError, frames.FrameError, hpack.HpackError):
            self._goaway(ErrorCode.PROTOCOL_ERROR)

    def _read_exact(self, count: int) -> bytes:
        chunks: list[bytes] = []
        remaining = count
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_settings(self) -> None:
        settings = (
            (frames.SettingsParameter.MAX_CONCURRENT_STREAMS, _MAX_CONCURRENT_STREAMS),
            (frames.SettingsParameter.MAX_FRAME_SIZE, _OUR_MAX_FRAME),
            (frames.SettingsParameter.ENABLE_PUSH, 0),
        )
        self.sock.sendall(frames.serialize(frames.SettingsFrame(0, Flag(0), settings)))

    # -- frame dispatch ---------------------------------------------------

    def _handle_frame(self, frame: frames.Frame) -> None:
        if isinstance(frame, frames.SettingsFrame):
            self._handle_settings(frame)
        elif isinstance(frame, (frames.HeadersFrame, frames.ContinuationFrame)):
            self._handle_header_block(frame)
        elif isinstance(frame, frames.WindowUpdateFrame):
            if frame.stream_id == 0:
                self.conn_window += frame.window_size_increment
        elif isinstance(frame, frames.PingFrame):
            if not frame.ack:
                self.sock.sendall(frames.serialize(frames.ping_ack(frame.opaque_data)))
        elif isinstance(frame, frames.RstStreamFrame):
            self.rst_count += 1
            self.blocks.pop(frame.stream_id, None)
            if self.rst_count > _MAX_RST_STREAMS:
                self._goaway(ErrorCode.ENHANCE_YOUR_CALM)
        elif isinstance(frame, frames.GoAwayFrame):
            self.running = False
        elif isinstance(frame, frames.DataFrame) and frame.data and frame.stream_id:
            # No request bodies on the GET/HEAD path: just keep the window open.
            self.sock.sendall(
                frames.serialize(frames.WindowUpdateFrame(0, Flag(0), len(frame.data)))
            )

    def _handle_settings(self, frame: frames.SettingsFrame) -> None:
        if frame.ack:
            return
        for ident, value in frame.settings:
            if ident == frames.SettingsParameter.INITIAL_WINDOW_SIZE:
                self.peer_window = value
        self.sock.sendall(frames.serialize(frames.settings_ack()))

    def _handle_header_block(self, frame: frames.HeadersFrame | frames.ContinuationFrame) -> None:
        stream_id = frame.stream_id
        block = self.blocks.get(stream_id)
        if block is None:
            if len(self.blocks) >= _MAX_CONCURRENT_STREAMS:
                self._reset(stream_id, ErrorCode.REFUSED_STREAM)
                return
            block = bytearray()
            self.blocks[stream_id] = block
        block += frame.header_block
        if len(block) > _MAX_HEADER_BLOCK:
            self._goaway(ErrorCode.ENHANCE_YOUR_CALM)
            return
        if frame.end_headers:
            self.blocks.pop(stream_id, None)
            headers = self.decoder.decode(bytes(block))
            self._dispatch(stream_id, headers)

    # -- request handling -------------------------------------------------

    def _dispatch(self, stream_id: int, headers: _HeaderList) -> None:
        pseudo = {name: value for name, value in headers if name.startswith(b":")}
        regular = {name: value for name, value in headers if not name.startswith(b":")}
        method = pseudo.get(b":method", b"").decode("latin-1")
        path = pseudo.get(b":path", b"/").decode("latin-1")

        if self.config.auth is not None and not self._authorized(regular):
            self._respond(stream_id, 401, [(b"www-authenticate", b'Basic realm="servery"')], b"")
            return
        if method not in {"GET", "HEAD"}:
            self._respond(stream_id, 405, [(b"allow", b"GET, HEAD")], b"")
            return

        status, headers_out, body = self._build_response(path)
        self._respond(stream_id, status, headers_out, body if method == "GET" else None)

    def _authorized(self, regular: dict[bytes, bytes]) -> bool:
        credential = self.handler._server.credential
        if credential is None:
            return True
        header = regular.get(b"authorization")
        return header is not None and credential.check_header(header.decode("latin-1"))

    def _build_response(self, url_path: str) -> tuple[int, _HeaderList, bytes]:
        fs_path = self.handler.translate_path(url_path)
        display = url_path.split("?", 1)[0].split("#", 1)[0]
        headers: _HeaderList = []
        if self.config.security_headers:
            headers.append((b"x-content-type-options", b"nosniff"))

        if os.path.isdir(fs_path):
            if not display.endswith("/"):
                return 301, [(b"location", (display + "/").encode("latin-1"))], b""
            try:
                body = listing.render(fs_path, display, show_hidden=self.config.show_hidden)
            except OSError:
                return self._error(404)
            if self.config.security_headers:
                headers.append((b"content-security-policy", b"default-src 'none'"))
            headers.append((b"content-type", b"text/html; charset=utf-8"))
            headers.append((b"content-length", str(len(body)).encode("ascii")))
            return 200, headers, body

        if not security.is_contained(self.handler._server.root_real, fs_path):
            return self._error(404)
        try:
            with open(fs_path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self._error(404)
        ctype = mimetypes.guess_file_type(fs_path)[0] or "application/octet-stream"
        headers.append((b"content-type", ctype.encode("latin-1")))
        headers.append((b"content-length", str(len(body)).encode("ascii")))
        return 200, headers, body

    @staticmethod
    def _error(status: int) -> tuple[int, _HeaderList, bytes]:
        body = str(status).encode("ascii")
        return status, [(b"content-type", b"text/plain"), (b"content-length", b"3")], body

    # -- response writing -------------------------------------------------

    def _respond(
        self, stream_id: int, status: int, headers: _HeaderList, body: bytes | None
    ) -> None:
        block = self.encoder.encode([(b":status", str(status).encode("ascii")), *headers])
        end_stream = not body
        flags = Flag.END_HEADERS | (Flag.END_STREAM if end_stream else Flag(0))
        self.sock.sendall(
            frames.build_header9(len(block), FrameType.HEADERS, flags, stream_id) + block
        )
        if body:
            self._write_data(stream_id, body)

    def _write_data(self, stream_id: int, body: bytes) -> None:
        offset = 0
        total = len(body)
        stream_window = self.peer_window or 0
        while offset < total:
            budget = min(
                _OUR_MAX_FRAME, total - offset, max(stream_window, 0), max(self.conn_window, 0)
            )
            if budget <= 0:
                if not self._pump_for_window():
                    return
                stream_window = self.peer_window or 0
                continue
            chunk = body[offset : offset + budget]
            last = offset + budget >= total
            flags = Flag.END_STREAM if last else Flag(0)
            self.sock.sendall(
                frames.build_header9(len(chunk), FrameType.DATA, flags, stream_id) + chunk
            )
            offset += budget
            stream_window -= budget
            self.conn_window -= budget

    def _pump_for_window(self) -> bool:
        """Blocked on flow control: read frames until the connection window opens."""
        data = self.rfile.read1(65536)
        if not data:
            return False
        self.reader.feed(data)
        for frame in self.reader:
            if isinstance(frame, frames.WindowUpdateFrame) and frame.stream_id == 0:
                self.conn_window += frame.window_size_increment
            else:
                self._handle_frame(frame)
        return self.conn_window > 0

    def _reset(self, stream_id: int, error: int) -> None:
        self.sock.sendall(frames.serialize(frames.RstStreamFrame(stream_id, Flag(0), error)))

    def _goaway(self, error: int) -> None:
        with contextlib.suppress(OSError):
            self.sock.sendall(frames.serialize(frames.GoAwayFrame(0, Flag(0), 0, error, b"")))
        self.running = False
