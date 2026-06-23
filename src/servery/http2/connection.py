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
import ssl
from typing import TYPE_CHECKING

from servery import _log, listing
from servery.handler import _CSP
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
        self.peer_window = (
            frames.SETTINGS_DEFAULTS[frames.SettingsParameter.INITIAL_WINDOW_SIZE] or 0
        )
        self.conn_window = 65535  # connection-level window default (RFC 9113 §6.9.2)
        self.stream_windows: dict[int, int] = {}
        self.rst_count = 0
        self.running = True

    def _stream_window(self, stream_id: int) -> int:
        return self.stream_windows.get(stream_id, self.peer_window)

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
        except (OSError, frames.FrameError, hpack.HpackError) as exc:
            _log.logger.debug("HTTP/2 connection error: %r", exc)
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
            else:
                self.stream_windows[frame.stream_id] = (
                    self._stream_window(frame.stream_id) + frame.window_size_increment
                )
        elif isinstance(frame, frames.PingFrame):
            if not frame.ack:
                self.sock.sendall(frames.serialize(frames.ping_ack(frame.opaque_data)))
        elif isinstance(frame, frames.RstStreamFrame):
            self.rst_count += 1
            self.blocks.pop(frame.stream_id, None)
            self.stream_windows.pop(frame.stream_id, None)
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
                if value > 0x7FFFFFFF:  # exceeds the flow-control max (RFC 9113 §6.5.2)
                    self._goaway(ErrorCode.FLOW_CONTROL_ERROR)
                    return
                self.peer_window = value
        self.sock.sendall(frames.serialize(frames.settings_ack()))

    def _handle_header_block(self, frame: frames.HeadersFrame | frames.ContinuationFrame) -> None:
        stream_id = frame.stream_id
        block = self.blocks.get(stream_id)
        if block is None:
            if isinstance(frame, frames.ContinuationFrame):
                # CONTINUATION must immediately follow HEADERS (RFC 9113 §6.10).
                self._goaway(ErrorCode.PROTOCOL_ERROR)
                return
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
        if b":method" not in pseudo or b":path" not in pseudo:
            self._reset(stream_id, ErrorCode.PROTOCOL_ERROR)  # malformed (RFC 9113 §8.3.1)
            return
        method = pseudo[b":method"].decode("latin-1")
        path = pseudo[b":path"].decode("latin-1")

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
            if isinstance(self.handler.connection, ssl.SSLSocket):  # h2 may also be h2c cleartext
                headers.append((b"strict-transport-security", b"max-age=63072000"))
        if self.config.cors:
            headers.append((b"access-control-allow-origin", b"*"))
        headers.append((b"cache-control", self.config.cache_control.encode("latin-1")))

        if os.path.isdir(fs_path):
            if not display.endswith("/"):
                return 301, [(b"location", (display + "/").encode("latin-1"))], b""
            try:
                body = listing.render(
                    fs_path,
                    display,
                    show_hidden=self.config.show_hidden,
                    per_page=listing.DEFAULT_PAGE_SIZE,
                )
            except OSError:
                return self._error(404)
            if self.config.security_headers:
                # The full CSP (style-src etc.) — "default-src 'none'" alone blocked
                # the listing's own inline styles, rendering it unstyled.
                headers.append((b"content-security-policy", _CSP.encode("latin-1")))
                headers.append((b"referrer-policy", b"no-referrer"))
            headers.append((b"content-type", b"text/html; charset=utf-8"))
            headers.append((b"content-length", str(len(body)).encode("ascii")))
            return 200, headers, body

        # translate_path() already ran the symlink-safe containment check and
        # returned "" for anything escaping the root — re-checking here would do a
        # second realpath() (the priciest non-I/O op) on every file request.
        if not fs_path:
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
        headers: _HeaderList = [
            (b"content-type", b"text/plain"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        return status, headers, body

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
        self.stream_windows.pop(stream_id, None)

    def _write_data(self, stream_id: int, body: bytes) -> None:
        offset = 0
        total = len(body)
        self.stream_windows.setdefault(stream_id, self.peer_window)
        while offset < total:
            budget = min(
                _OUR_MAX_FRAME,
                total - offset,
                max(self._stream_window(stream_id), 0),
                max(self.conn_window, 0),
            )
            if budget <= 0:
                if not self._pump_for_window(stream_id):
                    return  # peer never opened the window (socket timeout/stall budget hit)
                continue
            chunk = body[offset : offset + budget]
            last = offset + budget >= total
            flags = Flag.END_STREAM if last else Flag(0)
            self.sock.sendall(
                frames.build_header9(len(chunk), FrameType.DATA, flags, stream_id) + chunk
            )
            offset += budget
            self.stream_windows[stream_id] -= budget
            self.conn_window -= budget

    def _pump_for_window(self, stream_id: int) -> bool:
        """Blocked on flow control: read frames until this stream's window opens.

        Honors both stream- and connection-level WINDOW_UPDATE. Bounded so a peer
        that dribbles non-opening frames cannot pin the worker forever (the socket
        timeout also applies to each read).
        """
        for _ in range(1000):
            data = self.rfile.read1(65536)
            if not data:
                return False
            self.reader.feed(data)
            for frame in self.reader:
                self._handle_frame(frame)
                if not self.running:
                    return False
            if self._stream_window(stream_id) > 0 and self.conn_window > 0:
                return True
        return False

    def _reset(self, stream_id: int, error: int) -> None:
        self.sock.sendall(frames.serialize(frames.RstStreamFrame(stream_id, Flag(0), error)))

    def _goaway(self, error: int) -> None:
        _log.logger.debug("HTTP/2 GOAWAY error=%s", error)
        with contextlib.suppress(OSError):
            self.sock.sendall(frames.serialize(frames.GoAwayFrame(0, Flag(0), 0, error, b"")))
        self.running = False
