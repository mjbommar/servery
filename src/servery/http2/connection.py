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
import socket
import ssl
import threading
from typing import TYPE_CHECKING

from servery import _log, _response, _write, auth
from servery.http2 import frames, hpack
from servery.http2.frames import ErrorCode, Flag, FrameType

if TYPE_CHECKING:
    from servery.handler import ServeryHandler

_MAX_HEADER_BLOCK = 64 * 1024
_MAX_HEADER_LIST = 64 * 1024
_MAX_RST_STREAMS = 200
_MAX_CONTINUATION_FRAMES = 128
_MAX_CONTROL_FRAMES = 1000
_OUR_MAX_FRAME = 16384

_HeaderList = list[tuple[bytes, bytes]]


class _Outbound:
    """One bounded-memory response body scheduled across flow-control windows."""

    def __init__(self, stream_id: int, body: _response.ResponseBody) -> None:
        self.stream_id = stream_id
        self.body = body
        self.offset = 0
        self.total = len(body) if isinstance(body, bytes) else body.size

    @property
    def remaining(self) -> int:
        return self.total - self.offset

    def read(self, size: int) -> bytes:
        if isinstance(self.body, bytes):
            chunk = self.body[self.offset : self.offset + size]
        else:
            chunk = self.body.handle.read(size)
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        if isinstance(self.body, _response.FileBody):
            self.body.close()


class H2Connection:
    """Drives one HTTP/2 connection to completion."""

    def __init__(self, handler: ServeryHandler) -> None:
        self.handler = handler
        self.rfile = handler.rfile
        self.sock = handler.connection
        self.config = handler._server.config
        self.decoder = hpack.Decoder(max_header_list_size=_MAX_HEADER_LIST)
        self.encoder = hpack.Encoder()
        self.reader = frames.FrameReader(max_frame_size=_OUR_MAX_FRAME)
        self.blocks: dict[int, bytearray] = {}
        self._header_end_stream: dict[int, bool] = {}
        self._pending_headers: dict[int, _HeaderList] = {}
        self._continuation_stream: int | None = None
        self._continuation_frames = 0
        self.active_streams: set[int] = set()
        self.last_client_stream_id = 0
        self.outbound: dict[int, _Outbound] = {}
        self.peer_window = (
            frames.SETTINGS_DEFAULTS[frames.SettingsParameter.INITIAL_WINDOW_SIZE] or 0
        )
        self.conn_window = 65535  # connection-level window default (RFC 9113 §6.9.2)
        self.stream_windows: dict[int, int] = {}
        self.rst_count = 0
        self.control_count = 0
        self.running = True
        self._send_lock = threading.Lock()
        # Admission and GOAWAY's last-stream snapshot must be one atomic
        # boundary, including on free-threaded Python.
        self._drain_lock = threading.Lock()
        self._draining = threading.Event()
        self._last_accepted_stream_id = 0

    def _stream_window(self, stream_id: int) -> int:
        return self.stream_windows.get(stream_id, self.peer_window)

    def _send(self, data: bytes) -> None:
        with self._send_lock:
            timeout = self.config.write_timeout
            if timeout is None:
                self.sock.sendall(data)
                return
            with _write.socket_timeout(self.sock, timeout):
                self.sock.sendall(data)

    # -- main loop --------------------------------------------------------

    def run(self) -> None:
        if self._read_exact(len(frames.CONNECTION_PREFACE)) != frames.CONNECTION_PREFACE:
            return
        self._send_settings()
        self.handler._server.register_connection_drainer(self.sock, self._begin_draining)
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
                self._flush_outbound()
        except (OSError, frames.FrameError, hpack.HpackError) as exc:
            _log.logger.debug("HTTP/2 connection error: %r", exc)
            self._goaway(ErrorCode.PROTOCOL_ERROR)
        finally:
            self.handler._server.unregister_connection_drainer(self.sock)
            for response in self.outbound.values():
                response.close()

    def _begin_draining(self) -> None:
        """Advertise graceful shutdown while allowing already accepted streams."""
        with self._drain_lock:
            if self._draining.is_set():
                return
            self._draining.set()
            last_accepted = self._last_accepted_stream_id
            idle = not self.active_streams
        _log.logger.debug("HTTP/2 graceful GOAWAY last_stream_id=%s", last_accepted)
        self._send(
            frames.serialize(frames.GoAwayFrame(0, Flag(0), last_accepted, ErrorCode.NO_ERROR, b""))
        )
        if idle:
            # Wake read1() after the terminal control frame is on the wire.  An
            # idle H2 connection has no accepted work that could require further
            # client WINDOW_UPDATE frames.  SHUT_RD is sufficient to interrupt a
            # socket read on POSIX, but not reliably a buffered makefile() read on
            # Windows; the completed sendall keeps GOAWAY ordered before SHUT_RDWR.
            self.running = False
            with contextlib.suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)

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
            (frames.SettingsParameter.MAX_CONCURRENT_STREAMS, self.config.max_h2_streams),
            (frames.SettingsParameter.MAX_FRAME_SIZE, _OUR_MAX_FRAME),
            (frames.SettingsParameter.MAX_HEADER_LIST_SIZE, _MAX_HEADER_LIST),
            (frames.SettingsParameter.ENABLE_PUSH, 0),
        )
        self._send(frames.serialize(frames.SettingsFrame(0, Flag(0), settings)))

    # -- frame dispatch ---------------------------------------------------

    def _handle_frame(self, frame: frames.Frame) -> None:
        if self._continuation_stream is not None and not (
            isinstance(frame, frames.ContinuationFrame)
            and frame.stream_id == self._continuation_stream
        ):
            self._goaway(ErrorCode.PROTOCOL_ERROR)
            return
        if isinstance(frame, frames.SettingsFrame):
            if not self._control_allowed():
                return
            self._handle_settings(frame)
        elif isinstance(frame, (frames.HeadersFrame, frames.ContinuationFrame)):
            self._handle_header_block(frame)
        elif isinstance(frame, frames.WindowUpdateFrame):
            if frame.stream_id == 0:
                updated = self.conn_window + frame.window_size_increment
                if updated > 0x7FFFFFFF:
                    self._goaway(ErrorCode.FLOW_CONTROL_ERROR)
                    return
                self.conn_window = updated
            else:
                if frame.stream_id not in self.active_streams:
                    if frame.stream_id > self.last_client_stream_id:
                        self._goaway(ErrorCode.PROTOCOL_ERROR)
                    return  # WINDOW_UPDATE on a closed stream may be ignored
                updated = self._stream_window(frame.stream_id) + frame.window_size_increment
                if updated > 0x7FFFFFFF:
                    self._goaway(ErrorCode.FLOW_CONTROL_ERROR)
                    return
                self.stream_windows[frame.stream_id] = updated
        elif isinstance(frame, frames.PingFrame):
            if not self._control_allowed():
                return
            if not frame.ack:
                self._send(frames.serialize(frames.ping_ack(frame.opaque_data)))
        elif isinstance(frame, frames.RstStreamFrame):
            if frame.stream_id > self.last_client_stream_id:
                self._goaway(ErrorCode.PROTOCOL_ERROR)
                return
            self.rst_count += 1
            self._drop_stream(frame.stream_id)
            if self.rst_count > _MAX_RST_STREAMS:
                self._goaway(ErrorCode.ENHANCE_YOUR_CALM)
        elif isinstance(frame, frames.GoAwayFrame):
            self.running = False
        elif isinstance(frame, frames.DataFrame) and frame.stream_id:
            self._handle_data(frame)

    def _control_allowed(self) -> bool:
        """Budget acknowledgement-triggering control frames on this connection."""
        self.control_count += 1
        if self.control_count > _MAX_CONTROL_FRAMES:
            self._goaway(ErrorCode.ENHANCE_YOUR_CALM)
            return False
        return True

    def _handle_settings(self, frame: frames.SettingsFrame) -> None:
        if frame.ack:
            return
        for ident, value in frame.settings:
            if ident == frames.SettingsParameter.INITIAL_WINDOW_SIZE:
                if value > 0x7FFFFFFF:  # exceeds the flow-control max (RFC 9113 §6.5.2)
                    self._goaway(ErrorCode.FLOW_CONTROL_ERROR)
                    return
                delta = value - self.peer_window
                for stream_id, window in tuple(self.stream_windows.items()):
                    updated = window + delta
                    if not -(2**31) <= updated <= 0x7FFFFFFF:
                        self._goaway(ErrorCode.FLOW_CONTROL_ERROR)
                        return
                    self.stream_windows[stream_id] = updated
                self.peer_window = value
        self._send(frames.serialize(frames.settings_ack()))

    def _handle_header_block(self, frame: frames.HeadersFrame | frames.ContinuationFrame) -> None:
        stream_id = frame.stream_id
        if isinstance(frame, frames.ContinuationFrame):
            self._continuation_frames += 1
            if self._continuation_frames > _MAX_CONTINUATION_FRAMES:
                self._goaway(ErrorCode.ENHANCE_YOUR_CALM)
                return
        block = self.blocks.get(stream_id)
        if block is None:
            if isinstance(frame, frames.ContinuationFrame):
                # CONTINUATION must immediately follow HEADERS (RFC 9113 §6.10).
                self._goaway(ErrorCode.PROTOCOL_ERROR)
                return
            if stream_id % 2 == 0 or stream_id <= self.last_client_stream_id:
                self._goaway(ErrorCode.PROTOCOL_ERROR)
                return
            self.last_client_stream_id = stream_id
            with self._drain_lock:
                refuse_for_drain = self._draining.is_set() or self.handler._server.is_draining
                if not refuse_for_drain and len(self.active_streams) < self.config.max_h2_streams:
                    self.active_streams.add(stream_id)
                    self._last_accepted_stream_id = stream_id
            if refuse_for_drain:
                self._reset(stream_id, ErrorCode.REFUSED_STREAM)
                return
            if stream_id not in self.active_streams:
                self._reset(stream_id, ErrorCode.REFUSED_STREAM)
                return
            block = bytearray()
            self.blocks[stream_id] = block
            self._header_end_stream[stream_id] = frame.end_stream
            if not frame.end_headers:
                self._continuation_stream = stream_id
                self._continuation_frames = 0
        block += frame.header_block
        if len(block) > _MAX_HEADER_BLOCK:
            self._goaway(ErrorCode.ENHANCE_YOUR_CALM)
            return
        if frame.end_headers:
            self._continuation_stream = None
            self._continuation_frames = 0
            self.blocks.pop(stream_id, None)
            headers = self.decoder.decode(bytes(block))
            if self._header_end_stream.pop(stream_id, False):
                self._dispatch(stream_id, headers)
            else:
                self._pending_headers[stream_id] = headers

    def _handle_data(self, frame: frames.DataFrame) -> None:
        if frame.stream_id not in self.active_streams:
            if frame.stream_id > self.last_client_stream_id:
                self._goaway(ErrorCode.PROTOCOL_ERROR)
            else:
                self._reset(frame.stream_id, ErrorCode.STREAM_CLOSED)
            return
        if frame.data:
            increment = len(frame.data)
            self._send(frames.serialize(frames.WindowUpdateFrame(0, Flag(0), increment)))
            self._send(
                frames.serialize(frames.WindowUpdateFrame(frame.stream_id, Flag(0), increment))
            )
        if frame.end_stream:
            headers = self._pending_headers.pop(frame.stream_id, None)
            if headers is not None:
                self._dispatch(frame.stream_id, headers)

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
            self._respond(
                stream_id,
                401,
                [(b"www-authenticate", auth.WWW_AUTHENTICATE.encode("latin-1"))],
                b"",
            )
            return
        if method not in {"GET", "HEAD"}:
            self._respond(stream_id, 405, [(b"allow", b"GET, HEAD")], b"")
            return

        accept_encoding = regular.get(b"accept-encoding", b"").decode("latin-1")
        status, headers_out, body = self._build_response(path, accept_encoding, regular)
        if method == "HEAD" and isinstance(body, _response.FileBody):
            body.close()
        self._respond(stream_id, status, headers_out, body if method == "GET" else None)

    def _authorized(self, regular: dict[bytes, bytes]) -> bool:
        credential = self.handler._server.credential
        if credential is None:
            return True
        header = regular.get(b"authorization")
        return header is not None and credential.check_header(header.decode("latin-1"))

    def _build_response(
        self, url_path: str, accept_encoding: str, regular: dict[bytes, bytes]
    ) -> tuple[int, _HeaderList, _response.ResponseBody]:
        # translate_path() already ran the symlink-safe containment check and
        # returned "" for anything escaping the root (build_static maps that to a
        # 404) — re-checking would do a second realpath() on every file request.
        fs_path = self.handler.translate_path(url_path)
        display = url_path.split("?", 1)[0].split("#", 1)[0]
        # h2 may be cleartext (h2c); only assert HSTS over a real TLS socket.
        tls = isinstance(self.handler.connection, ssl.SSLSocket)
        inm = regular.get(b"if-none-match")
        ims = regular.get(b"if-modified-since")
        return _response.build_static(
            self.config,
            fs_path,
            display,
            accept_encoding,
            tls=tls,
            if_none_match=inm.decode("latin-1") if inm is not None else None,
            if_modified_since=ims.decode("latin-1") if ims is not None else None,
            compression_cache=self.handler._server.compression_cache,
        )

    # -- response writing -------------------------------------------------

    def _respond(
        self,
        stream_id: int,
        status: int,
        headers: _HeaderList,
        body: _response.ResponseBody | None,
    ) -> None:
        block = self.encoder.encode([(b":status", str(status).encode("ascii")), *headers])
        body_size = 0 if body is None else (len(body) if isinstance(body, bytes) else body.size)
        end_stream = body_size == 0
        flags = Flag.END_HEADERS | (Flag.END_STREAM if end_stream else Flag(0))
        try:
            self._send(
                frames.build_header9(len(block), FrameType.HEADERS, flags, stream_id) + block
            )
        except BaseException:
            if isinstance(body, _response.FileBody):
                body.close()
            raise
        if end_stream:
            self._complete_stream(stream_id)
        elif body is not None:
            self.stream_windows.setdefault(stream_id, self.peer_window)
            self.outbound[stream_id] = _Outbound(stream_id, body)

    def _flush_outbound(self) -> None:
        """Send ready DATA fairly, at most one frame per stream per pass."""
        made_progress = True
        while made_progress and self.conn_window > 0:
            made_progress = False
            for stream_id, response in tuple(self.outbound.items()):
                budget = min(
                    _OUR_MAX_FRAME,
                    response.remaining,
                    max(self._stream_window(stream_id), 0),
                    max(self.conn_window, 0),
                )
                if budget <= 0:
                    continue
                try:
                    chunk = response.read(budget)
                except OSError:
                    self._reset(stream_id, ErrorCode.INTERNAL_ERROR)
                    continue
                if not chunk:
                    self._reset(stream_id, ErrorCode.INTERNAL_ERROR)
                    continue
                last = response.remaining == 0
                flags = Flag.END_STREAM if last else Flag(0)
                self._send(
                    frames.build_header9(len(chunk), FrameType.DATA, flags, stream_id) + chunk
                )
                self.stream_windows[stream_id] -= len(chunk)
                self.conn_window -= len(chunk)
                made_progress = True
                if last:
                    self._complete_stream(stream_id)

    def _complete_stream(self, stream_id: int) -> None:
        response = self.outbound.pop(stream_id, None)
        if response is not None:
            response.close()
        with self._drain_lock:
            self.active_streams.discard(stream_id)
            drained = self._draining.is_set() and not self.active_streams
        self.stream_windows.pop(stream_id, None)
        self._pending_headers.pop(stream_id, None)
        if drained:
            self.running = False

    def _drop_stream(self, stream_id: int) -> None:
        self.blocks.pop(stream_id, None)
        self._header_end_stream.pop(stream_id, None)
        self._complete_stream(stream_id)

    def _reset(self, stream_id: int, error: int) -> None:
        self._send(frames.serialize(frames.RstStreamFrame(stream_id, Flag(0), error)))
        self._drop_stream(stream_id)

    def _goaway(self, error: int) -> None:
        _log.logger.debug("HTTP/2 GOAWAY error=%s", error)
        with contextlib.suppress(OSError):
            self._send(
                frames.serialize(
                    frames.GoAwayFrame(0, Flag(0), self.last_client_stream_id, error, b"")
                )
            )
        self.running = False
